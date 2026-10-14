# api/subscription.py
"""
LawNeeds Subscription API
Handles subscription management, payments, and usage tracking.
"""

from fastapi import HTTPException, APIRouter, Request, Body, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any
import logging
import uuid
from decimal import Decimal

from services.subscription_service import SubscriptionService
from services.subscription_constants import (
    SubscriptionTier, TOP_UP_MIN_MINUTES, 
    TOP_UP_MAX_MINUTES, SUBSCRIPTION_PLANS, get_upgrade_options,
    FREEMIUM_DAILY_PASS_PRICE_GBP, calculate_topup_price,
    REVENUECAT_TOP_UP_PRODUCTS, REVENUECAT_DAILY_PASS_PRODUCT_ID,
    REVENUECAT_DAILY_PASS_PRODUCT_IDS,
    FIRST_MONTH_DISCOUNT_PERCENT, get_first_month_price, BillingPeriod,
    get_topup_product_id, get_daily_pass_product_id
)
from services.revenuecat_service import RevenueCatService
from services.firebase_notification_service import FirebaseNotificationService
from services.models import DeviceToken

logger = logging.getLogger(__name__)


# ============================================
# PYDANTIC MODELS
# ============================================

class PlanInfo(BaseModel):
    """Subscription plan information"""
    id: str
    name: str
    tier: str
    billing_period: Optional[str]
    price_gbp: float
    minutes_included: int
    voice_notes_limit: Optional[int]
    uploads_limit: Optional[int]
    docgen_limit: Optional[int]
    max_image_size_mb: int
    max_doc_pages: int
    unlimited_ai_queries: bool
    rollover_enabled: bool
    priority_support: bool
    early_access: bool
    free_consultation_minutes: Optional[int]
    # First-month discount (50% off for monthly plans)
    first_month_price_gbp: Optional[float] = None
    first_month_discount_percent: Optional[int] = None


class SubscriptionInfo(BaseModel):
    """User's subscription information"""
    plan_id: str
    plan_name: str
    tier: str
    billing_period: Optional[str]
    status: str
    current_period_end: Optional[str]
    auto_renew: bool
    is_first_month: bool


class MinutesInfo(BaseModel):
    """User's minutes balance"""
    total: int
    used: int
    available: int
    rollover: int
    top_up: int
    top_ups_this_month: int
    usage_percent: float
    alert_threshold_reached: bool


class UsageSummaryResponse(BaseModel):
    """Complete usage summary"""
    subscription: SubscriptionInfo
    minutes: MinutesInfo
    monthly_usage: Dict[str, int]
    limits: Dict[str, Any]
    features: Dict[str, bool]
    freemium: Optional[Dict[str, Any]] = None


class UpgradeRequest(BaseModel):
    """Request to upgrade subscription"""
    new_plan_id: str = Field(..., description="ID of the plan to upgrade to")
    payment_method_id: Optional[str] = Field(None, description="Deprecated: handled by RevenueCat SDK")


class TopUpRequest(BaseModel):
    """Request to purchase top-up minutes"""
    minutes: int = Field(..., ge=TOP_UP_MIN_MINUTES, le=TOP_UP_MAX_MINUTES, 
                        description=f"Minutes to purchase ({TOP_UP_MIN_MINUTES}-{TOP_UP_MAX_MINUTES})")
    os_type: str = Field(..., description="Operating system type: 'ios' or 'android'")
    payment_method_id: Optional[str] = Field(None, description="Deprecated: handled by RevenueCat SDK")


class DailyPassRequest(BaseModel):
    """Request to purchase daily unlimited query pass"""
    os_type: str = Field(..., description="Operating system type: 'ios' or 'android'")
    payment_method_id: Optional[str] = Field(None, description="Deprecated: handled by RevenueCat SDK")


class CancelSubscriptionRequest(BaseModel):
    """Request to cancel subscription"""
    immediate: bool = Field(False, description="Cancel immediately or at period end")
    reason: Optional[str] = Field(None, description="Cancellation reason")


class SubscribeRequest(BaseModel):
    """Request to subscribe to a plan"""
    plan_id: str = Field(..., description="ID of the plan to subscribe to")
    payment_method_id: Optional[str] = Field(None, description="Deprecated: handled by RevenueCat SDK")


class DowngradeRequest(BaseModel):
    """Request to downgrade subscription"""
    new_plan_id: str = Field(..., description="ID of the plan to downgrade to")


class SubscriptionSyncRequest(BaseModel):
    """
    Request to sync subscription state from RevenueCat SDK.
    Called on app login to reconcile backend state with RevenueCat.
    """
    user_id: str = Field(..., description="LawNeeds user ID (UUID)")
    app_user_id: str = Field(..., description="RevenueCat anonymous user ID")
    updated_at: Optional[str] = Field(None, description="ISO timestamp of last update")
    plan: Optional[str] = Field(None, description="Current plan: 'standard', 'premium', or null for freemium")
    entitlement_id: Optional[str] = Field(None, description="RevenueCat entitlement ID (e.g., 'standard_access')")
    package_id: Optional[str] = Field(None, description="RevenueCat package ID (e.g., 'standard:standard-monthly')")
    is_active: bool = Field(..., description="Whether the subscription is currently active")
    expires_at: Optional[str] = Field(None, description="ISO timestamp of subscription expiration")
    will_renew: bool = Field(False, description="Whether subscription will auto-renew")
    last_event: Optional[str] = Field(None, description="Last RevenueCat event type")
    # Only present on the sync call fired immediately after Purchases.purchasePackage()
    # (see PurchaseData in the frontend). Absent on plain login/reconciliation syncs.
    # Used to send the purchase confirmation email + build the PDF receipt.
    price_paid: Optional[float] = Field(None, description="Price paid at purchase time, from RevenueCat package.product")
    currency: Optional[str] = Field(None, description="Currency code at purchase time, from RevenueCat package.product")
    transaction_id: Optional[str] = Field(None, description="RevenueCat transaction identifier from the purchase")


class ConsumableSyncRequest(BaseModel):
    """
    Request to sync consumable purchases (Daily Pass or Top-up) from RevenueCat SDK.
    Called immediately after a successful purchase to update the backend state.
    """
    user_id: str = Field(..., description="LawNeeds user ID (UUID)")
    transaction_id: str = Field(..., description="RevenueCat transaction ID")
    product_id: str = Field(..., description="RevenueCat product ID (e.g., 'daily-pass', 'topup-5')")
    purchase_date: Optional[str] = Field(None, description="ISO timestamp of purchase")


class ConsumableSyncResponse(BaseModel):
    """Response from consumable sync"""
    success: bool
    message: str
    synced: bool
    consumable_type: str  # 'daily_pass' or 'top_up'
    details: Dict[str, Any]


class UsageHistoryResponse(BaseModel):
    """Usage history item"""
    id: str
    action_type: str
    minutes_deducted: int
    status: str
    created_at: str
    file_name: Optional[str]
    duration_seconds: Optional[int]
    page_count: Optional[int]


# ============================================
# SUBSCRIPTION API
# ============================================

class SubscriptionAPI:
    def __init__(self, *, app_name, db, auth):
        self.app_name = app_name
        self.db = db
        self.auth = auth
    
    def get_router(self):
        router = APIRouter()
        
        # ============================================
        # PLANS & INFO ENDPOINTS
        # ============================================
        
        @router.get("/subscription/plans", response_model=List[PlanInfo])
        async def get_available_plans():
            """Get all available subscription plans with first-month discount info"""
            try:
                plans = []
                for plan_id, plan_data in SUBSCRIPTION_PLANS.items():
                    # Calculate first-month discount for monthly plans
                    first_month_price = None
                    first_month_discount = None
                    
                    if plan_data["billing_period"] == BillingPeriod.MONTHLY.value:
                        first_month_price = float(get_first_month_price(plan_id))
                        first_month_discount = FIRST_MONTH_DISCOUNT_PERCENT
                    
                    plans.append(PlanInfo(
                        id=plan_data["id"],
                        name=plan_data["name"],
                        tier=plan_data["tier"],
                        billing_period=plan_data["billing_period"],
                        price_gbp=float(plan_data["price_gbp"]),
                        minutes_included=plan_data["minutes_included"],
                        voice_notes_limit=plan_data["voice_notes_limit"],
                        uploads_limit=plan_data["uploads_limit"],
                        docgen_limit=plan_data["docgen_limit"],
                        max_image_size_mb=plan_data["max_image_size_mb"],
                        max_doc_pages=plan_data["max_doc_pages"],
                        unlimited_ai_queries=plan_data["unlimited_ai_queries"],
                        rollover_enabled=plan_data["rollover_enabled"],
                        priority_support=plan_data["priority_support"],
                        early_access=plan_data["early_access"],
                        free_consultation_minutes=plan_data.get("free_consultation_minutes"),
                        first_month_price_gbp=first_month_price,
                        first_month_discount_percent=first_month_discount,
                    ))
                
                # Sort by display order
                plans.sort(key=lambda x: SUBSCRIPTION_PLANS[x.id]["display_order"])
                return plans
                
            except Exception as e:
                logger.error(f"Error getting plans: {e}")
                raise HTTPException(status_code=500, detail="Error retrieving plans")
        
        @router.get("/subscription/my-plan")
        async def get_my_subscription(request: Request):
            """Get user's current subscription and usage"""
            try:
                token_data = self.auth.verify_request_token(request)
                user_id = token_data.get("user_id")
                
                if not user_id:
                    raise HTTPException(status_code=401, detail="Invalid token")
                
                with self.db.get_session() as session:
                    service = SubscriptionService(session)
                    summary = service.get_usage_summary(uuid.UUID(user_id))
                    
                    # Debug logging to trace subscription data
                    logger.info(f"[my-plan] User {user_id}: plan_id={summary['subscription']['plan_id']}, "
                               f"tier={summary['subscription']['tier']}, status={summary['subscription']['status']}")
                    
                    return UsageSummaryResponse(
                        subscription=SubscriptionInfo(**summary['subscription']),
                        minutes=MinutesInfo(**summary['minutes']),
                        monthly_usage=summary['monthly_usage'],
                        limits=summary['limits'],
                        features=summary['features'],
                        freemium=summary.get('freemium')
                    )
                    
            except HTTPException:
                raise
            except ValueError as e:
                # User doesn't exist
                logger.error(f"User validation error: {e}")
                raise HTTPException(status_code=404, detail="User not found")
            except Exception as e:
                logger.error(f"Error getting subscription: {e}")
                raise HTTPException(status_code=500, detail="Error retrieving subscription")
        
        @router.get("/subscription/upgrade-options")
        async def get_upgrade_options_endpoint(request: Request):
            """Get available upgrade options for current plan"""
            try:
                token_data = self.auth.verify_request_token(request)
                user_id = token_data.get("user_id")
                
                if not user_id:
                    raise HTTPException(status_code=401, detail="Invalid token")
                
                with self.db.get_session() as session:
                    service = SubscriptionService(session)
                    subscription = service.get_or_create_subscription(uuid.UUID(user_id))
                    
                    upgrades = get_upgrade_options(subscription.plan_id)
                    
                    return JSONResponse(
                        status_code=200,
                        content={
                            "current_plan": subscription.plan_id,
                            "upgrades": [
                                {
                                    "id": plan["id"],
                                    "name": plan["name"],
                                    "tier": plan["tier"],
                                    "billing_period": plan["billing_period"],
                                    "price_gbp": float(plan["price_gbp"]),
                                    "minutes_included": plan["minutes_included"],
                                }
                                for plan in upgrades
                            ]
                        }
                    )
                    
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error getting upgrade options: {e}")
                raise HTTPException(status_code=500, detail="Error retrieving upgrade options")
        
        # ============================================
        # SUBSCRIPTION MANAGEMENT
        # ============================================
        
        @router.post("/subscription/upgrade")
        async def upgrade_subscription(request: Request, upgrade_data: UpgradeRequest):
            """Upgrade to a new subscription plan"""
            try:
                token_data = self.auth.verify_request_token(request)
                user_id = token_data.get("user_id")
                
                if not user_id:
                    raise HTTPException(status_code=401, detail="Invalid token")

                with self.db.get_session() as session:
                    service = SubscriptionService(session)
                    plan = SUBSCRIPTION_PLANS.get(upgrade_data.new_plan_id)
                    if not plan or plan["tier"] == SubscriptionTier.FREEMIUM.value:
                        raise HTTPException(status_code=400, detail="Invalid paid plan")

                    current_sub = service.get_or_create_subscription(uuid.UUID(user_id))
                    if current_sub.plan_id == upgrade_data.new_plan_id:
                        return JSONResponse(
                            status_code=200,
                            content={
                                "success": True,
                                "message": "Already on this plan",
                                "plan_id": current_sub.plan_id,
                                "tier": current_sub.tier
                            }
                        )

                    return JSONResponse(
                        status_code=200,
                        content={
                            "success": True,
                            "message": "Purchase via RevenueCat SDK. We'll update after webhook confirmation.",
                            "plan_id": plan["id"],
                            "product_id": plan["id"],
                            "price_gbp": float(plan["price_gbp"])
                        }
                    )
                        
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error upgrading subscription: {e}")
                raise HTTPException(status_code=500, detail="Error upgrading subscription")
        
        @router.post("/subscription/cancel")
        async def cancel_subscription(request: Request, cancel_data: CancelSubscriptionRequest):
            """Cancel user subscription"""
            try:
                token_data = self.auth.verify_request_token(request)
                user_id = token_data.get("user_id")
                
                if not user_id:
                    raise HTTPException(status_code=401, detail="Invalid token")
                
                with self.db.get_session() as session:
                    service = SubscriptionService(session)
                    current_sub = service.get_or_create_subscription(uuid.UUID(user_id))

                    logger.info(f"User {user_id} requested cancellation. Reason: {cancel_data.reason}")

                    if current_sub.plan_id == "freemium":
                        return JSONResponse(
                            status_code=200,
                            content={
                                "success": True,
                                "message": "No active paid subscription to cancel."
                            }
                        )

                    return JSONResponse(
                        status_code=200,
                        content={
                            "success": True,
                            "message": "Manage cancellations in the App Store / Play Store. We'll sync after confirmation."
                        }
                    )
                        
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error cancelling subscription: {e}")
                raise HTTPException(status_code=500, detail="Error cancelling subscription")
        
        # ============================================
        # TOP-UPS & PURCHASES
        # ============================================
        
        @router.post("/subscription/top-up")
        async def purchase_topup(request: Request, topup_data: TopUpRequest):
            """Purchase top-up minutes"""
            try:
                token_data = self.auth.verify_request_token(request)
                user_id = token_data.get("user_id")
                
                if not user_id:
                    raise HTTPException(status_code=401, detail="Invalid token")

                # Validate OS type
                os_type = topup_data.os_type.lower()
                if os_type not in ("ios", "android"):
                    raise HTTPException(status_code=400, detail="Invalid os_type. Must be 'ios' or 'android'")

                # Get platform-specific product ID
                product_id = get_topup_product_id(topup_data.minutes, os_type)

                if not product_id:
                    raise HTTPException(status_code=400, detail="Invalid top-up amount")

                amount_gbp = calculate_topup_price(topup_data.minutes)

                return JSONResponse(
                    status_code=200,
                    content={
                        "success": True,
                        "message": "Purchase via RevenueCat SDK. We'll update after webhook confirmation.",
                        "product_id": product_id,
                        "os_type": os_type,
                        "minutes": topup_data.minutes,
                        "amount_gbp": float(amount_gbp)
                    }
                )
                        
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error purchasing top-up: {e}")
                raise HTTPException(status_code=500, detail="Error purchasing top-up")
        
        @router.post("/subscription/daily-pass")
        async def purchase_daily_pass(request: Request, pass_data: DailyPassRequest):
            """Purchase daily unlimited query pass (freemium only)"""
            try:
                token_data = self.auth.verify_request_token(request)
                user_id = token_data.get("user_id")
                
                if not user_id:
                    raise HTTPException(status_code=401, detail="Invalid token")

                # Validate OS type
                os_type = pass_data.os_type.lower()
                if os_type not in ("ios", "android"):
                    raise HTTPException(status_code=400, detail="Invalid os_type. Must be 'ios' or 'android'")
                
                with self.db.get_session() as session:
                    service = SubscriptionService(session)
                    
                    # Get subscription for customer ID
                    subscription = service.get_or_create_subscription(uuid.UUID(user_id))
                    
                    # Only freemium users can purchase daily pass
                    if subscription.tier != SubscriptionTier.FREEMIUM.value:
                        raise HTTPException(
                            status_code=400, 
                            detail="Daily pass is only for freemium users. You have unlimited queries."
                        )
                    
                    amount_gbp = Decimal(str(FREEMIUM_DAILY_PASS_PRICE_GBP))

                    # Get platform-specific product ID
                    product_id = get_daily_pass_product_id(os_type)

                    return JSONResponse(
                        status_code=200,
                        content={
                            "success": True,
                            "message": "Purchase via RevenueCat SDK. We'll update after webhook confirmation.",
                            "product_id": product_id,
                            "os_type": os_type,
                            "amount_gbp": float(amount_gbp)
                        }
                    )
                        
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error purchasing daily pass: {e}")
                raise HTTPException(status_code=500, detail="Error purchasing daily pass")
        
        @router.post("/subscription/ad-watched")
        async def record_ad_watched(request: Request):
            """
            Record that user watched an ad (grants 6 bonus queries).
            
            Returns updated query status including:
            - bonus_queries: Number of queries granted
            - queries_remaining: Total queries now available
            - queries_locked: Whether queries are still locked
            - unlock_at: When queries will unlock (if locked)
            - window_ends: When the current 6-hour window ends
            """
            try:
                token_data = self.auth.verify_request_token(request)
                user_id = token_data.get("user_id")
                
                if not user_id:
                    raise HTTPException(status_code=401, detail="Invalid token")
                
                with self.db.get_session() as session:
                    service = SubscriptionService(session)
                    
                    success, message, status = service.grant_ad_bonus(uuid.UUID(user_id))
                    
                    if success:
                        return JSONResponse(
                            status_code=200,
                            content={
                                "success": True,
                                "message": message,
                                **status  # Include full status: bonus_queries, queries_remaining, queries_locked, unlock_at, window_ends
                            }
                        )
                    else:
                        raise HTTPException(status_code=400, detail=message)
                        
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error recording ad watch: {e}")
                raise HTTPException(status_code=500, detail="Error recording ad watch")
        
        # ============================================
        # FREEMIUM STATUS ENDPOINTS
        # ============================================
        
        @router.get("/subscription/freemium/status")
        async def get_freemium_status(request: Request):
            """
            Get comprehensive freemium user status.
            
            Returns query limits, daily pass status, trial allowances for freemium users.
            For paid users, returns tier info and indicates not freemium.
            """
            try:
                token_data = self.auth.verify_request_token(request)
                user_id = token_data.get("user_id")
                
                if not user_id:
                    raise HTTPException(status_code=401, detail="Invalid token")
                
                with self.db.get_session() as session:
                    service = SubscriptionService(session)
                    status = service.get_freemium_status(uuid.UUID(user_id))
                    
                    return JSONResponse(status_code=200, content=status)
                    
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error getting freemium status: {e}")
                raise HTTPException(status_code=500, detail="Error retrieving freemium status")
        
        @router.get("/subscription/freemium/query-status")
        async def get_query_status(request: Request):
            """
            Check if user can make a query.
            
            For freemium users: Returns query allowance status.
            For paid users: Returns allowed=True (unlimited queries).
            """
            try:
                token_data = self.auth.verify_request_token(request)
                user_id = token_data.get("user_id")
                
                if not user_id:
                    raise HTTPException(status_code=401, detail="Invalid token")
                
                with self.db.get_session() as session:
                    service = SubscriptionService(session)
                    status = service.check_query_limit_for_freemium(uuid.UUID(user_id))
                    
                    return JSONResponse(status_code=200, content=status)
                    
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error checking query status: {e}")
                raise HTTPException(status_code=500, detail="Error checking query status")
        
        @router.get("/subscription/freemium/trial/status")
        async def get_trial_status(request: Request):
            """
            Get trial allowance status for freemium user.
            
            Returns voice notes, uploads, and docgen trial usage.
            """
            try:
                token_data = self.auth.verify_request_token(request)
                user_id = token_data.get("user_id")
                
                if not user_id:
                    raise HTTPException(status_code=401, detail="Invalid token")
                
                with self.db.get_session() as session:
                    service = SubscriptionService(session)
                    status = service.get_freemium_status(uuid.UUID(user_id))
                    
                    if not status.get('is_freemium', False):
                        return JSONResponse(
                            status_code=200,
                            content={
                                'tier': status.get('tier'),
                                'is_freemium': False,
                                'message': 'Trial status only applicable to freemium users'
                            }
                        )
                    
                    return JSONResponse(
                        status_code=200,
                        content={
                            'tier': 'freemium',
                            'trial_allowances': status.get('trial_allowances', {}),
                            'is_freemium': True
                        }
                    )
                    
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error getting trial status: {e}")
                raise HTTPException(status_code=500, detail="Error retrieving trial status")
        
        # ============================================
        # USAGE & ANALYTICS
        # ============================================
        
        @router.get("/subscription/usage-history", response_model=List[UsageHistoryResponse])
        async def get_usage_history(
            request: Request,
            limit: int = Query(50, ge=1, le=100, description="Number of records to return")
        ):
            """Get usage history for user"""
            try:
                token_data = self.auth.verify_request_token(request)
                user_id = token_data.get("user_id")
                
                if not user_id:
                    raise HTTPException(status_code=401, detail="Invalid token")
                
                with self.db.get_session() as session:
                    service = SubscriptionService(session)
                    history = service.get_usage_history(uuid.UUID(user_id), limit=limit)
                    
                    return [UsageHistoryResponse(**item) for item in history]
                    
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error getting usage history: {e}")
                raise HTTPException(status_code=500, detail="Error retrieving usage history")
        
        @router.get("/subscription/events")
        async def get_subscription_events(
            request: Request,
            limit: int = Query(20, ge=1, le=50, description="Number of events to return")
        ):
            """Get subscription events for user"""
            try:
                token_data = self.auth.verify_request_token(request)
                user_id = token_data.get("user_id")
                
                if not user_id:
                    raise HTTPException(status_code=401, detail="Invalid token")
                
                with self.db.get_session() as session:
                    service = SubscriptionService(session)
                    events = service.get_subscription_events(uuid.UUID(user_id), limit=limit)
                    
                    return JSONResponse(
                        status_code=200,
                        content={
                            "events": events
                        }
                    )
                    
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error getting subscription events: {e}")
                raise HTTPException(status_code=500, detail="Error retrieving events")
        
        # ============================================
        # SUBSCRIBE, DOWNGRADE & REACTIVATE
        # ============================================
        
        @router.post("/subscription/subscribe")
        async def subscribe_to_plan(request: Request, subscribe_data: SubscribeRequest):
            """Subscribe to a new plan (for freemium users)"""
            try:
                token_data = self.auth.verify_request_token(request)
                user_id = token_data.get("user_id")
                
                if not user_id:
                    raise HTTPException(status_code=401, detail="Invalid token")
                
                with self.db.get_session() as session:
                    service = SubscriptionService(session)
                    plan = SUBSCRIPTION_PLANS.get(subscribe_data.plan_id)
                    if not plan or plan["tier"] == SubscriptionTier.FREEMIUM.value:
                        raise HTTPException(status_code=400, detail="Invalid paid plan")

                    current_sub = service.get_or_create_subscription(uuid.UUID(user_id))
                    if current_sub.plan_id != "freemium":
                        return JSONResponse(
                            status_code=200,
                            content={
                                "success": True,
                                "message": "Already subscribed. Use upgrade endpoint for plan changes.",
                                "plan_id": current_sub.plan_id,
                                "tier": current_sub.tier
                            }
                        )

                    return JSONResponse(
                        status_code=200,
                        content={
                            "success": True,
                            "message": "Purchase via RevenueCat SDK. We'll update after webhook confirmation.",
                            "plan_id": plan["id"],
                            "product_id": plan["id"],
                            "price_gbp": float(plan["price_gbp"])
                        }
                    )
                        
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error subscribing: {e}")
                raise HTTPException(status_code=500, detail="Error creating subscription")
        
        @router.post("/subscription/downgrade")
        async def downgrade_subscription(request: Request, downgrade_data: DowngradeRequest):
            """Downgrade to a lower tier (takes effect at period end)"""
            try:
                token_data = self.auth.verify_request_token(request)
                user_id = token_data.get("user_id")
                
                if not user_id:
                    raise HTTPException(status_code=401, detail="Invalid token")
                
                with self.db.get_session() as session:
                    service = SubscriptionService(session)
                    
                    success, message, subscription = service.downgrade_subscription(
                        user_id=uuid.UUID(user_id),
                        new_plan_id=downgrade_data.new_plan_id
                    )
                    
                    if success:
                        return JSONResponse(
                            status_code=200,
                            content={
                                "success": True,
                                "message": message,
                                "current_plan_id": subscription.plan_id,
                                "pending_downgrade_to": subscription.pending_downgrade_to,
                                "effective_at": service.serialize_utc_datetime(subscription.current_period_end)
                            }
                        )
                    else:
                        raise HTTPException(status_code=400, detail=message)
                        
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error downgrading subscription: {e}")
                raise HTTPException(status_code=500, detail="Error downgrading subscription")
        
        @router.post("/subscription/reactivate")
        async def reactivate_subscription(request: Request):
            """Reactivate a cancelled subscription"""
            try:
                token_data = self.auth.verify_request_token(request)
                user_id = token_data.get("user_id")
                
                if not user_id:
                    raise HTTPException(status_code=401, detail="Invalid token")
                
                with self.db.get_session() as session:
                    service = SubscriptionService(session)
                    
                    success, message, subscription = service.reactivate_subscription(
                        user_id=uuid.UUID(user_id)
                    )
                    
                    if success:
                        return JSONResponse(
                            status_code=200,
                            content={
                                "success": True,
                                "message": message,
                                "plan_id": subscription.plan_id,
                                "status": subscription.status
                            }
                        )
                    else:
                        raise HTTPException(status_code=400, detail=message)
                        
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error reactivating subscription: {e}")
                raise HTTPException(status_code=500, detail="Error reactivating subscription")
        
        # ============================================
        # SUBSCRIPTION SYNC (APP → BACKEND)
        # ============================================
        
        @router.post("/subscription/sync")
        async def sync_subscription(request: Request, sync_data: SubscriptionSyncRequest):
            """
            Sync subscription state from RevenueCat SDK.
            
            Called on app login to reconcile backend state with RevenueCat.
            Backend ALWAYS trusts RevenueCat entitlement over its own state.
            
            This acts as a fallback/reconciliation mechanism in case webhooks
            were missed or delayed.
            """
            try:
                token_data = self.auth.verify_request_token(request)
                auth_user_id = token_data.get("user_id")
                
                if not auth_user_id:
                    raise HTTPException(status_code=401, detail="Invalid token")
                
                # Verify the user_id in payload matches the authenticated user
                if sync_data.user_id != auth_user_id:
                    logger.warning(f"Sync user_id mismatch: payload={sync_data.user_id}, auth={auth_user_id}")
                    raise HTTPException(status_code=403, detail="User ID mismatch")
                
                with self.db.get_session() as session:
                    service = SubscriptionService(session)
                    
                    success, message, updated_subscription = service.sync_from_revenuecat(
                        user_id=uuid.UUID(auth_user_id),
                        app_user_id=sync_data.app_user_id,
                        plan=sync_data.plan,
                        entitlement_id=sync_data.entitlement_id,
                        package_id=sync_data.package_id,
                        is_active=sync_data.is_active,
                        expires_at=sync_data.expires_at,
                        will_renew=sync_data.will_renew,
                        last_event=sync_data.last_event,
                        price_paid=sync_data.price_paid,
                        currency=sync_data.currency,
                        transaction_id=sync_data.transaction_id,
                    )
                    
                    if success:
                        # Get updated usage summary
                        summary = service.get_usage_summary(uuid.UUID(auth_user_id))
                        
                        # Debug logging to trace sync results
                        logger.info(f"[sync] User {auth_user_id}: synced to plan_id={updated_subscription.plan_id}, "
                                   f"tier={updated_subscription.tier}, status={updated_subscription.status}, "
                                   f"message={message}")
                        
                        return JSONResponse(
                            status_code=200,
                            content={
                                "success": True,
                                "message": message,
                                "synced": True,
                                "subscription": {
                                    "plan_id": updated_subscription.plan_id,
                                    "tier": updated_subscription.tier,
                                    "status": updated_subscription.status,
                                    "expires_at": service.serialize_utc_datetime(updated_subscription.current_period_end),
                                    "will_renew": updated_subscription.auto_renew
                                },
                                "minutes": summary.get("minutes", {}),
                                "features": summary.get("features", {})
                            }
                        )
                    else:
                        return JSONResponse(
                            status_code=200,
                            content={
                                "success": False,
                                "message": message,
                                "synced": False
                            }
                        )
                        
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error syncing subscription: {e}")
                raise HTTPException(status_code=500, detail="Error syncing subscription")
        
        @router.post("/subscription/sync-consumables")
        async def sync_consumables(request: Request, sync_data: ConsumableSyncRequest):
            """
            Sync consumable purchases (Daily Pass or Top-up) from RevenueCat SDK.
            
            Called immediately after a successful RevenueCat purchase to update the
            backend state without waiting for the webhook.
            
            This endpoint:
            - For Daily Pass: Activates unlimited queries until midnight
            - For Top-up: Adds purchased minutes to user's account
            
            Provides immediate feedback and state update for better UX.
            """
            try:
                token_data = self.auth.verify_request_token(request)
                auth_user_id = token_data.get("user_id")
                
                if not auth_user_id:
                    raise HTTPException(status_code=401, detail="Invalid token")
                
                # Verify the user_id in payload matches the authenticated user
                if sync_data.user_id != auth_user_id:
                    logger.warning(f"Consumable sync user_id mismatch: payload={sync_data.user_id}, auth={auth_user_id}")
                    raise HTTPException(status_code=403, detail="User ID mismatch")
                
                with self.db.get_session() as session:
                    service = SubscriptionService(session)
                    
                    success, message, details = service.sync_consumable_from_client(
                        user_id=uuid.UUID(auth_user_id),
                        transaction_id=sync_data.transaction_id,
                        product_id=sync_data.product_id,
                        purchase_date=sync_data.purchase_date
                    )
                    
                    # Determine consumable type (check against all daily pass variants)
                    consumable_type = "daily_pass" if sync_data.product_id in REVENUECAT_DAILY_PASS_PRODUCT_IDS else "top_up"
                    
                    if success:
                        # Get updated usage summary
                        summary = service.get_usage_summary(uuid.UUID(auth_user_id))
                        
                        return JSONResponse(
                            status_code=200,
                            content={
                                "success": True,
                                "message": message,
                                "synced": True,
                                "consumable_type": consumable_type,
                                "details": details,
                                "minutes": summary.get("minutes", {}),
                                "freemium": summary.get("freemium", {})
                            }
                        )
                    else:
                        return JSONResponse(
                            status_code=200,
                            content={
                                "success": False,
                                "message": message,
                                "synced": False,
                                "consumable_type": consumable_type,
                                "details": details
                            }
                        )
                        
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error syncing consumables: {e}")
                raise HTTPException(status_code=500, detail="Error syncing consumables")
        
        # ============================================
        # REVENUECAT WEBHOOKS
        # ============================================
        
        @router.post("/subscription/webhook")
        async def revenuecat_webhook(request: Request):
            """
            Handle RevenueCat webhook events.
            """
            rc_service = RevenueCatService()
            is_valid, message = rc_service.verify_webhook(request.headers)
            if not is_valid:
                logger.error(f"RevenueCat webhook verification failed: {message}")
                raise HTTPException(status_code=401, detail=message)

            try:
                payload = await request.json()
            except Exception as e:
                logger.error(f"Invalid RevenueCat webhook payload: {e}")
                raise HTTPException(status_code=400, detail="Invalid payload")

            event = rc_service.parse_event(payload)
            event_type = event.get("type")

            logger.info(f"Received RevenueCat webhook: {event_type}")

            with self.db.get_session() as session:
                service = SubscriptionService(session)
                try:
                    success, msg, should_notify, subscription_notification = service.handle_revenuecat_event(event)
                    if not success:
                        logger.error(f"RevenueCat webhook handling failed: {msg}")
                    
                    # Send top-up suggestion notification if needed
                    if should_notify:
                        app_user_id = event.get("app_user_id")
                        if app_user_id:
                            try:
                                user_uuid = uuid.UUID(app_user_id)
                                await _send_top_up_suggestion_notification(session, user_uuid)
                            except Exception as e:
                                logger.warning(f"Failed to send top-up notification: {e}")

                    # Send subscription-level notification (currently: subscription_renewed only --
                    # subscription_expired is handled by jobs/subscription_jobs.py's hourly job instead,
                    # to avoid a duplicate send. See _handle_revenuecat_subscription_event's docstring.)
                    if subscription_notification:
                        try:
                            device_tokens = subscription_notification.get("device_tokens")
                            notif_type = subscription_notification.get("notification_type")
                            if device_tokens and notif_type == "subscription_renewed":
                                firebase_service = FirebaseNotificationService()
                                await firebase_service.send_subscription_renewed_notification(
                                    device_tokens=device_tokens,
                                    plan_name=subscription_notification.get("plan_name"),
                                    subscription_id=subscription_notification.get("subscription_id"),
                                )
                        except Exception as e:
                            logger.warning(f"Failed to send subscription renewal notification: {e}")
                except Exception as e:
                    logger.error(f"Error processing RevenueCat webhook {event_type}: {e}")
                    # Return 200 to avoid repeated retries; log for follow-up.

            return JSONResponse(status_code=200, content={"received": True})
        
        async def _send_top_up_suggestion_notification(session, user_id: uuid.UUID):
            """Send notification suggesting upgrade after 2 top-ups in same month"""
            try:
                service = SubscriptionService(session)
                device_tokens = service.get_user_device_tokens(user_id)
                
                if not device_tokens:
                    return
                
                suggested_plan = service.get_suggested_plan(user_id)
                firebase_service = FirebaseNotificationService()
                
                title = "Consider Upgrading Your Plan 📈"
                body = f"You've topped up twice this month. {suggested_plan} may suit you better with more included minutes."
                
                data = {
                    'type': 'top_up_suggestion',
                    'screen': 'SubscriptionPlans',
                    'action': 'view_plans',
                    'suggested_plan': suggested_plan
                }
                
                await firebase_service.send_notification_to_multiple(
                    device_tokens=device_tokens,
                    title=title,
                    body=body,
                    data=data
                )
                logger.info(f"Sent top-up suggestion notification to user {user_id}")
            except Exception as e:
                logger.error(f"Error sending top-up suggestion notification: {e}")
        
        @router.post("/subscription/send-annual-reminder")
        async def send_annual_reminder(request: Request):
            """Send annual subscriber benefits reminder notification (admin/manual trigger)"""
            try:
                token_data = self.auth.verify_request_token(request)
                user_id = token_data.get("user_id")
                
                if not user_id:
                    raise HTTPException(status_code=401, detail="Invalid token")
                
                with self.db.get_session() as session:
                    service = SubscriptionService(session)
                    success, notification_data = service.send_annual_subscriber_benefits_reminder(uuid.UUID(user_id))
                    
                    if not success or not notification_data:
                        return JSONResponse(
                            status_code=200,
                            content={
                                "success": False,
                                "message": "User is not eligible for annual benefits reminder"
                            }
                        )
                    
                    # Send notification
                    firebase_service = FirebaseNotificationService()
                    result = await firebase_service.send_notification_to_multiple(
                        device_tokens=notification_data['device_tokens'],
                        title=notification_data['title'],
                        body=notification_data['body'],
                        data=notification_data['data']
                    )
                    
                    return JSONResponse(
                        status_code=200,
                        content={
                            "success": True,
                            "message": "Annual benefits reminder sent",
                            "notification_result": result
                        }
                    )
                    
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error sending annual reminder: {e}")
                raise HTTPException(status_code=500, detail="Error sending annual reminder")
        
        # ============================================
        # ADMIN: MANUAL JOB TRIGGERS
        # ============================================
        
        @router.post("/subscription/admin/run-job/{job_name}")
        async def run_job_manually(request: Request, job_name: str):
            """
            Manually trigger a subscription job (admin only).
            
            Available jobs:
            - reset_daily_passes: Reset expired daily passes
            - reset_ad_counters: Reset daily ad watch counters
            - cleanup_trials: Cleanup expired trials
            - process_expiring: Process expiring subscriptions
            - usage_alerts: Send usage alert notifications
            - expire_credits: Expire annual consultation credits
            """
            try:
                token_data = self.auth.verify_request_token(request)
                user_id = token_data.get("user_id")
                
                if not user_id:
                    raise HTTPException(status_code=401, detail="Invalid token")
                
                # TODO: Add admin role check here
                # For now, allow any authenticated user (should be restricted in production)
                
                try:
                    from jobs.subscription_jobs import (
                        reset_expired_daily_passes,
                        reset_daily_ad_watch_counters,
                        cleanup_expired_trials,
                        process_expiring_subscriptions,
                        send_usage_alert_notifications,
                        expire_annual_consultation_credits,
                        send_annual_credit_reminders,
                    )
                except ImportError as e:
                    raise HTTPException(status_code=503, detail=f"Jobs module not available: {e}")
                
                job_map = {
                    'reset_daily_passes': reset_expired_daily_passes,
                    'reset_ad_counters': reset_daily_ad_watch_counters,
                    'cleanup_trials': cleanup_expired_trials,
                    'process_expiring': process_expiring_subscriptions,
                    'usage_alerts': send_usage_alert_notifications,
                    'expire_credits': expire_annual_consultation_credits,
                    'annual_reminders': send_annual_credit_reminders,
                }
                
                if job_name not in job_map:
                    raise HTTPException(
                        status_code=400, 
                        detail=f"Unknown job: {job_name}. Available: {list(job_map.keys())}"
                    )
                
                # Run the job
                job_func = job_map[job_name]
                await job_func(self.db)
                
                logger.info(f"Admin triggered job: {job_name} by user {user_id}")
                
                return JSONResponse(
                    status_code=200,
                    content={
                        "success": True,
                        "message": f"Job '{job_name}' executed successfully",
                        "job": job_name
                    }
                )
                
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error running job {job_name}: {e}")
                raise HTTPException(status_code=500, detail=f"Error running job: {str(e)}")
        
        @router.get("/subscription/admin/scheduler-status")
        async def get_scheduler_status(request: Request):
            """
            Get the current status of the subscription scheduler.
            """
            try:
                token_data = self.auth.verify_request_token(request)
                user_id = token_data.get("user_id")
                
                if not user_id:
                    raise HTTPException(status_code=401, detail="Invalid token")
                
                try:
                    from jobs import get_scheduler
                    scheduler = get_scheduler()
                    
                    if scheduler is None:
                        return JSONResponse(
                            status_code=200,
                            content={
                                "scheduler_running": False,
                                "message": "Scheduler not initialized"
                            }
                        )
                    
                    jobs_info = []
                    for job in scheduler.get_jobs():
                        jobs_info.append({
                            "id": job.id,
                            "name": job.name,
                            "next_run": job.next_run_time.isoformat() if job.next_run_time else None,
                            "trigger": str(job.trigger),
                        })
                    
                    return JSONResponse(
                        status_code=200,
                        content={
                            "scheduler_running": scheduler.running,
                            "jobs_count": len(jobs_info),
                            "jobs": jobs_info
                        }
                    )
                    
                except ImportError:
                    return JSONResponse(
                        status_code=200,
                        content={
                            "scheduler_running": False,
                            "message": "Scheduler module not available"
                        }
                    )
                    
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error getting scheduler status: {e}")
                raise HTTPException(status_code=500, detail="Error getting scheduler status")
        
        # ============================================
        # ANALYTICS ENDPOINTS
        # ============================================
        
        @router.get("/subscription/analytics/trends")
        async def get_user_usage_trends(
            request: Request,
            days: int = Query(30, ge=7, le=90, description="Number of days to analyze")
        ):
            """
            Get usage trends for the authenticated user.
            
            Returns:
            - Daily usage breakdown
            - Usage by action type
            - Comparison with previous period
            """
            try:
                token_data = self.auth.verify_request_token(request)
                user_id = token_data.get("user_id")
                
                if not user_id:
                    raise HTTPException(status_code=401, detail="Invalid token")
                
                from services.analytics_service import AnalyticsService
                
                with self.db.get_session() as session:
                    analytics = AnalyticsService(session)
                    trends = analytics.user_get_usage_trends(uuid.UUID(user_id), days)
                    
                    return JSONResponse(
                        status_code=200,
                        content={
                            "success": True,
                            "data": trends
                        }
                    )
                    
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error getting usage trends: {e}")
                raise HTTPException(status_code=500, detail="Error getting usage trends")
        
        @router.get("/subscription/analytics/spending")
        async def get_user_spending_summary(request: Request):
            """
            Get spending summary for the authenticated user.
            
            Returns:
            - Top-up history and totals
            """
            try:
                token_data = self.auth.verify_request_token(request)
                user_id = token_data.get("user_id")
                
                if not user_id:
                    raise HTTPException(status_code=401, detail="Invalid token")
                
                from services.analytics_service import AnalyticsService
                
                with self.db.get_session() as session:
                    analytics = AnalyticsService(session)
                    spending = analytics.user_get_spending_summary(uuid.UUID(user_id))
                    
                    return JSONResponse(
                        status_code=200,
                        content={
                            "success": True,
                            "data": spending
                        }
                    )
                    
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error getting spending summary: {e}")
                raise HTTPException(status_code=500, detail="Error getting spending summary")
        
        # ============================================
        # ADMIN ANALYTICS ENDPOINTS (Minimal/Extensible)
        # ============================================
        
        @router.get("/subscription/admin/analytics/overview")
        async def admin_get_subscription_overview(request: Request):
            """
            [Admin] Get high-level subscription metrics.
            
            Returns:
            - Total users by tier
            - Active subscriptions count
            - Subscription status breakdown
            """
            try:
                token_data = self.auth.verify_request_token(request)
                user_id = token_data.get("user_id")
                
                if not user_id:
                    raise HTTPException(status_code=401, detail="Invalid token")
                
                # TODO: Add admin role check when admin system is implemented
                # For now, any authenticated user can access (adjust as needed)
                
                from services.analytics_service import AnalyticsService
                
                with self.db.get_session() as session:
                    analytics = AnalyticsService(session)
                    overview = analytics.admin_get_subscription_overview()
                    
                    return JSONResponse(
                        status_code=200,
                        content={
                            "success": True,
                            "data": overview
                        }
                    )
                    
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error getting admin overview: {e}")
                raise HTTPException(status_code=500, detail="Error getting admin overview")
        
        @router.get("/subscription/admin/analytics/usage")
        async def admin_get_usage_metrics(
            request: Request,
            days: int = Query(30, ge=1, le=365, description="Number of days to analyze")
        ):
            """
            [Admin] Get usage metrics for the last N days.
            
            Returns:
            - Total actions by type
            - Minutes consumed
            - Top-up statistics
            """
            try:
                token_data = self.auth.verify_request_token(request)
                user_id = token_data.get("user_id")
                
                if not user_id:
                    raise HTTPException(status_code=401, detail="Invalid token")
                
                # TODO: Add admin role check when admin system is implemented
                
                from services.analytics_service import AnalyticsService
                
                with self.db.get_session() as session:
                    analytics = AnalyticsService(session)
                    metrics = analytics.admin_get_usage_metrics(days)
                    
                    return JSONResponse(
                        status_code=200,
                        content={
                            "success": True,
                            "data": metrics
                        }
                    )
                    
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error getting admin usage metrics: {e}")
                raise HTTPException(status_code=500, detail="Error getting admin usage metrics")
        
        @router.get("/subscription/admin/analytics/events")
        async def admin_get_recent_events(
            request: Request,
            limit: int = Query(50, ge=1, le=200, description="Maximum events to return")
        ):
            """
            [Admin] Get recent subscription events for monitoring.
            
            Returns:
            - List of recent subscription events with user info
            """
            try:
                token_data = self.auth.verify_request_token(request)
                user_id = token_data.get("user_id")
                
                if not user_id:
                    raise HTTPException(status_code=401, detail="Invalid token")
                
                # TODO: Add admin role check when admin system is implemented
                
                from services.analytics_service import AnalyticsService
                
                with self.db.get_session() as session:
                    analytics = AnalyticsService(session)
                    events = analytics.admin_get_recent_events(limit)
                    
                    return JSONResponse(
                        status_code=200,
                        content={
                            "success": True,
                            "data": events,
                            "count": len(events)
                        }
                    )
                    
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error getting admin events: {e}")
                raise HTTPException(status_code=500, detail="Error getting admin events")
        
        # ============================================
        # ANNUAL BENEFITS ENDPOINTS
        # ============================================
        
        @router.get("/subscription/annual-credit")
        async def get_annual_consultation_credit(request: Request):
            """
            Get user's annual consultation credit status (for annual subscribers).
            
            Returns:
            - Credit details if user has annual subscription
            - null if user is not on annual plan or has no credit
            """
            try:
                token_data = self.auth.verify_request_token(request)
                user_id = token_data.get("user_id")
                
                if not user_id:
                    raise HTTPException(status_code=401, detail="Invalid token")
                
                from services.models import AnnualConsultationCredit, UserSubscription
                
                with self.db.get_session() as session:
                    # Get subscription
                    subscription = session.query(UserSubscription).filter(
                        UserSubscription.user_id == uuid.UUID(user_id)
                    ).first()
                    
                    if not subscription or subscription.billing_period != BillingPeriod.ANNUAL.value:
                        return JSONResponse(
                            status_code=200,
                            content={
                                "success": True,
                                "has_annual_credit": False,
                                "message": "Annual consultation credits are only available for annual subscribers",
                                "credit": None
                            }
                        )
                    
                    # Get active credit
                    from datetime import datetime, timezone
                    now = datetime.now(timezone.utc)
                    
                    credit = session.query(AnnualConsultationCredit).filter(
                        AnnualConsultationCredit.user_id == uuid.UUID(user_id),
                        AnnualConsultationCredit.expired == False,
                        AnnualConsultationCredit.valid_until > now
                    ).order_by(AnnualConsultationCredit.created_at.desc()).first()
                    
                    if not credit:
                        return JSONResponse(
                            status_code=200,
                            content={
                                "success": True,
                                "has_annual_credit": False,
                                "message": "No active annual consultation credit found",
                                "credit": None
                            }
                        )
                    
                    return JSONResponse(
                        status_code=200,
                        content={
                            "success": True,
                            "has_annual_credit": True,
                            "credit": {
                                "id": str(credit.id),
                                "total_minutes": credit.consultation_minutes,
                                "minutes_remaining": credit.minutes_remaining,
                                "minutes_used": credit.consultation_minutes - credit.minutes_remaining,
                                "used_at": credit.used_at.isoformat() if credit.used_at else None,
                                "valid_from": credit.valid_from.isoformat() if credit.valid_from else None,
                                "valid_until": credit.valid_until.isoformat() if credit.valid_until else None,
                                "is_used": credit.minutes_remaining == 0,
                            }
                        }
                    )
                    
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error getting annual credit: {e}")
                raise HTTPException(status_code=500, detail="Error getting annual credit")
        
        return router
