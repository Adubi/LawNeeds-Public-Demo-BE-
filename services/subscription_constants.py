# services/subscription_constants.py
"""
LawNeeds Subscription System - Constants and Enums
Based on: LAWNEEDS MINUTES-BASED SYSTEM specification
"""

from enum import Enum
from decimal import Decimal
from typing import Dict, Any, Optional


# ============================================
# ENUMS
# ============================================

class SubscriptionTier(str, Enum):
    """User subscription tiers"""
    FREEMIUM = "freemium"
    EXPRESS = "express"
    STANDARD = "standard"
    PREMIUM = "premium"


class BillingPeriod(str, Enum):
    """Billing period options"""
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    ANNUAL = "annual"


class SubscriptionStatus(str, Enum):
    """Subscription status states"""
    ACTIVE = "active"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    PAST_DUE = "past_due"
    TRIALING = "trialing"
    PENDING = "pending"


class UsageActionType(str, Enum):
    """Types of actions that consume minutes or count toward limits"""
    VOICE_NOTE = "voice_note"
    UPLOAD = "upload"
    DOCGEN = "docgen"
    QUERY = "query"


class BlockedReason(str, Enum):
    """Reasons an action might be blocked"""
    INSUFFICIENT_MINUTES = "insufficient_minutes"
    MONTHLY_CAP_EXCEEDED = "monthly_cap_exceeded"
    FILE_SIZE_EXCEEDED = "file_size_exceeded"
    PAGE_LIMIT_EXCEEDED = "page_limit_exceeded"
    QUERY_LIMIT_EXCEEDED = "query_limit_exceeded"
    TRIAL_EXPIRED = "trial_expired"
    SUBSCRIPTION_INACTIVE = "subscription_inactive"
    DAILY_LIMIT_REACHED = "daily_limit_reached"
    UPGRADE_REQUIRED = "upgrade_required"


class PaymentStatus(str, Enum):
    """Payment transaction statuses"""
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REFUNDED = "refunded"


# ============================================
# SUBSCRIPTION PLAN CONFIGURATIONS
# ============================================

SUBSCRIPTION_PLANS: Dict[str, Dict[str, Any]] = {
    "freemium": {
        "id": "freemium",
        "name": "Basic",
        "tier": SubscriptionTier.FREEMIUM.value,
        "billing_period": None,
        "price_gbp": Decimal("0.00"),
        "minutes_included": 0,
        # Service limits (trial only for first month)
        "voice_notes_limit": 2,  # Trial only - 2/month
        "uploads_limit": 2,  # Trial only - 2/month
        "docgen_limit": 2,  # Trial only - 2/month
        # File size caps (promotional/trial limits)
        "max_image_size_mb": 10,
        "max_doc_pages": 4,
        # Docgen page limit for freemium
        "max_docgen_pages": 2,
        # Features
        "unlimited_ai_queries": False,
        "rollover_enabled": False,
        "priority_support": False,
        "early_access": False,
        # Display order for UI
        "display_order": 0,
        # Stripe
        "stripe_price_id": None,
    },
    "express-daily": {
        "id": "express-daily",
        "name": "Express",
        "tier": SubscriptionTier.EXPRESS.value,
        "billing_period": BillingPeriod.DAILY.value,
        "price_gbp": Decimal("4.99"),
        "minutes_included": 0,  # No minutes - Express is query-only
        # Service limits
        "voice_notes_limit": 4,
        "uploads_limit": 4,  # Shared image/PDF upload allowance
        "docgen_limit": 0,  # Not available on Express
        # File size caps (same as Basic)
        "max_image_size_mb": 10,
        "max_doc_pages": 4,
        # Docgen page limit
        "max_docgen_pages": 0,
        # Features - Only unlimited AI queries
        "unlimited_ai_queries": True,  # Unlimited queries until 12am
        "rollover_enabled": False,  # No rollover - expires at midnight
        "priority_support": False,
        "early_access": False,
        # Display order for UI
        "display_order": 1,
        # Stripe
        "stripe_price_id": None,
        # Express-specific: expires at midnight
        "expires_at_midnight": True,
    },
    "standard-weekly": {
        "id": "standard-weekly",
        "name": "Standard Weekly",
        "tier": SubscriptionTier.STANDARD.value,
        "billing_period": BillingPeriod.WEEKLY.value,
        "price_gbp": Decimal("9.99"),
        "minutes_included": 10,
        # Service limits
        "voice_notes_limit": 3,  # 3/week
        "uploads_limit": 3,  # 3/week
        "docgen_limit": 3,  # 3/week
        # File size caps
        "max_image_size_mb": 25,
        "max_doc_pages": 8,
        # Docgen page limit (no limit for standard)
        "max_docgen_pages": None,
        # Features
        "unlimited_ai_queries": True,
        "rollover_enabled": False,  # Weekly doesn't rollover
        "priority_support": False,
        "early_access": False,
        # Display order for UI
        "display_order": 2,
        # Stripe
        "stripe_price_id": None,  # To be set after Stripe setup
    },
    "standard-monthly": {
        "id": "standard-monthly",
        "name": "Standard Monthly",
        "tier": SubscriptionTier.STANDARD.value,
        "billing_period": BillingPeriod.MONTHLY.value,
        "price_gbp": Decimal("24.99"),
        "minutes_included": 30,
        # Service limits
        "voice_notes_limit": 12,  # 12/month
        "uploads_limit": 12,  # 12/month
        "docgen_limit": 12,  # 12/month
        # File size caps
        "max_image_size_mb": 25,
        "max_doc_pages": 8,
        # Docgen page limit
        "max_docgen_pages": None,
        # Features
        "unlimited_ai_queries": True,
        "rollover_enabled": True,  # Monthly rollover on renewal
        "priority_support": False,
        "early_access": False,
        # Display order for UI
        "display_order": 3,
        # Stripe
        "stripe_price_id": None,
    },
    "standard-annual": {
        "id": "standard-annual",
        "name": "Standard Annual",
        "tier": SubscriptionTier.STANDARD.value,
        "billing_period": BillingPeriod.ANNUAL.value,
        "price_gbp": Decimal("99.99"),
        "minutes_included": 140,
        # Service limits
        "voice_notes_limit": 120,  # 120/year
        "uploads_limit": 120,  # 120/year
        "docgen_limit": 120,  # 120/year
        # File size caps
        "max_image_size_mb": 25,
        "max_doc_pages": 8,
        # Docgen page limit
        "max_docgen_pages": None,
        # Features
        "unlimited_ai_queries": True,
        "rollover_enabled": True,  # Roll-over allowed
        "priority_support": False,
        "early_access": False,
        # Display order for UI
        "display_order": 4,
        # Bonus: Free consultation
        "free_consultation_minutes": 10,
        # Stripe
        "stripe_price_id": None,
    },
    "premium-monthly": {
        "id": "premium-monthly",
        "name": "Premium Monthly",
        "tier": SubscriptionTier.PREMIUM.value,
        "billing_period": BillingPeriod.MONTHLY.value,
        "price_gbp": Decimal("44.99"),
        "minutes_included": 50,
        # Service limits
        "voice_notes_limit": None,  # Unlimited
        "uploads_limit": 25,
        "docgen_limit": 25,
        # File size caps (higher for premium)
        "max_image_size_mb": 50,
        "max_doc_pages": 25,
        # Docgen page limit
        "max_docgen_pages": None,
        # Features
        "unlimited_ai_queries": True,
        "rollover_enabled": True,  # Monthly rollover on renewal
        "priority_support": True,
        "early_access": True,
        # Display order for UI
        "display_order": 5,
        # Stripe
        "stripe_price_id": None,
    },
    "premium-annual": {
        "id": "premium-annual",
        "name": "Premium Annual",
        "tier": SubscriptionTier.PREMIUM.value,
        "billing_period": BillingPeriod.ANNUAL.value,
        "price_gbp": Decimal("199.99"),
        "minutes_included": 330,
        # Service limits
        "voice_notes_limit": None,  # Unlimited
        "uploads_limit": 250,  # 250/year
        "docgen_limit": 250,  # 250/year
        # File size caps (higher for premium)
        "max_image_size_mb": 50,
        "max_doc_pages": 25,
        # Docgen page limit
        "max_docgen_pages": None,
        # Features
        "unlimited_ai_queries": True,
        "rollover_enabled": True,  # Roll-over allowed
        "priority_support": True,
        "early_access": True,
        # Display order for UI
        "display_order": 6,
        # Bonus: Free consultation
        "free_consultation_minutes": 20,
        # Stripe
        "stripe_price_id": None,
    },
}

# ============================================
# REVENUECAT MAPPINGS
# ============================================

REVENUECAT_ENTITLEMENT_TIER_MAP: Dict[str, str] = {
    "express_access": SubscriptionTier.EXPRESS.value,
    "standard_access": SubscriptionTier.STANDARD.value,
    "premium_access": SubscriptionTier.PREMIUM.value,
}

# Plan product IDs are aligned with plan IDs.
REVENUECAT_SUBSCRIPTION_PRODUCTS: Dict[str, str] = {
    plan_id: plan_id
    for plan_id in SUBSCRIPTION_PLANS.keys()
    if plan_id != "freemium"
}

# Platform-agnostic product IDs (used for webhook handling)
# Note: Android uses hyphens (topup-5), iOS uses underscores (topup_5)
REVENUECAT_TOP_UP_PRODUCTS: Dict[str, int] = {
    # Android format (hyphens)
    "topup-5": 5,
    "topup-10": 10,
    "topup-15": 15,
    "topup-20": 20,
    "topup-25": 25,
    "topup-30": 30,
    # iOS format (underscores, no suffix)
    "topup_5": 5,
    "topup_10": 10,
    "topup_15": 15,
    "topup_20": 20,
    "topup_25": 25,
    "topup_30": 30,
    # iOS product IDs (with suffix)
    "topup_5_ios": 5,
    "topup_10_ios": 10,
    "topup_15_ios": 15,
    "topup_20_ios": 20,
    "topup_25_ios": 25,
    "topup_30_ios": 30,
    # Android product IDs (with suffix)
    "topup_5_android": 5,
    "topup_10_android": 10,
    "topup_15_android": 15,
    "topup_20_android": 20,
    "topup_25_android": 25,
    "topup_30_android": 30,
}

# Express Daily product IDs (replaces the old daily-pass concept)
REVENUECAT_EXPRESS_DAILY_PRODUCT_ID = "express-daily"

# All valid Express Daily product IDs (for webhook/sync handling)
# Android uses hyphens, iOS uses underscores
REVENUECAT_EXPRESS_DAILY_PRODUCT_IDS = {
    "express-daily",      # Android format
    "express_daily",      # iOS format (no suffix)
    "express_daily_ios",  # iOS with suffix
    "express_daily_android",  # Android with suffix
}

# Legacy: Keep daily-pass mappings for backward compatibility
REVENUECAT_DAILY_PASS_PRODUCT_ID = "express-daily"  # Now points to Express Daily

# All valid daily pass product IDs (for webhook/sync handling) - includes legacy
REVENUECAT_DAILY_PASS_PRODUCT_IDS = {
    "express-daily",
    "express_daily_ios",
    "express_daily_android",
    # Legacy product IDs (backward compatibility)
    # Android: daily-pass, iOS: daily_pass
    "daily-pass",        # Android daily pass
    "daily_pass",        # iOS daily pass
    "daily_pass_ios",    # Alternative iOS format
    "daily_pass_android",  # Alternative Android format
}

# Platform-specific product IDs
REVENUECAT_TOP_UP_PRODUCTS_IOS: Dict[int, str] = {
    5: "topup_5_ios",
    10: "topup_10_ios",
    15: "topup_15_ios",
    20: "topup_20_ios",
    25: "topup_25_ios",
    30: "topup_30_ios",
}

REVENUECAT_TOP_UP_PRODUCTS_ANDROID: Dict[int, str] = {
    5: "topup_5_android",
    10: "topup_10_android",
    15: "topup_15_android",
    20: "topup_20_android",
    25: "topup_25_android",
    30: "topup_30_android",
}

# Express Daily platform-specific product IDs
REVENUECAT_EXPRESS_DAILY_PRODUCT_ID_IOS = "express_daily_ios"
REVENUECAT_EXPRESS_DAILY_PRODUCT_ID_ANDROID = "express_daily_android"

# Legacy aliases (backward compatibility)
REVENUECAT_DAILY_PASS_PRODUCT_ID_IOS = "express_daily_ios"
REVENUECAT_DAILY_PASS_PRODUCT_ID_ANDROID = "express_daily_android"


def get_topup_product_id(minutes: int, os_type: str) -> Optional[str]:
    """Get the platform-specific top-up product ID for the given minutes."""
    os_type_lower = os_type.lower()
    if os_type_lower == "ios":
        return REVENUECAT_TOP_UP_PRODUCTS_IOS.get(minutes)
    elif os_type_lower == "android":
        return REVENUECAT_TOP_UP_PRODUCTS_ANDROID.get(minutes)
    return None


def get_express_daily_product_id(os_type: str) -> str:
    """Get the platform-specific Express Daily product ID."""
    os_type_lower = os_type.lower()
    if os_type_lower == "ios":
        return REVENUECAT_EXPRESS_DAILY_PRODUCT_ID_IOS
    elif os_type_lower == "android":
        return REVENUECAT_EXPRESS_DAILY_PRODUCT_ID_ANDROID
    return REVENUECAT_EXPRESS_DAILY_PRODUCT_ID  # Fallback


def get_daily_pass_product_id(os_type: str) -> str:
    """Get the platform-specific daily pass product ID.
    
    Note: This is now an alias for get_express_daily_product_id() for backward compatibility.
    """
    return get_express_daily_product_id(os_type)


# ============================================
# MINUTES DEDUCTION RULES
# ============================================

# Voice note rules
VOICE_NOTE_MAX_DURATION_SECONDS = 300  # 5 minutes max
VOICE_NOTE_MAX_DURATION_MINUTES = 5

# Upload rules
UPLOAD_FLAT_MINUTES = 1  # 1 minute per upload

# Document generation rules (minutes by page count)
DOCGEN_MINUTES_BY_PAGES: Dict[tuple, int] = {
    (1, 2): 2,   # 1-2 pages = 2 minutes
    (3, 5): 4,   # 3-5 pages = 4 minutes
    (6, 10): 6,  # 6-10 pages = 6 minutes
}


def get_docgen_minutes(page_count: int) -> int:
    """Calculate minutes to deduct for document generation based on page count"""
    for (min_pages, max_pages), minutes in DOCGEN_MINUTES_BY_PAGES.items():
        if min_pages <= page_count <= max_pages:
            return minutes
    # Default to highest tier for pages > 10
    return 6


def calculate_voice_note_minutes(duration_seconds: int) -> int:
    """
    Calculate minutes to deduct for voice note.
    Rounds up to nearest minute, max 5 minutes.
    """
    import math
    minutes = math.ceil(duration_seconds / 60)
    return min(minutes, VOICE_NOTE_MAX_DURATION_MINUTES)


# ============================================
# FREEMIUM LIMITS
# ============================================

FREEMIUM_QUERIES_PER_WINDOW = 10  # 10 queries per window
FREEMIUM_WINDOW_HOURS = 6  # 6-hour window
FREEMIUM_AD_BONUS_QUERIES = 6  # 6 bonus queries for watching an ad

# Express Daily pricing
EXPRESS_DAILY_PRICE_GBP = Decimal("4.99")  # £4.99 for Express Daily unlimited queries
FREEMIUM_DAILY_PASS_PRICE_GBP = EXPRESS_DAILY_PRICE_GBP  # Backward compatibility alias

# Trial allowances (first month only)
FREEMIUM_TRIAL_VOICE_NOTES = 2
FREEMIUM_TRIAL_UPLOADS = 2
FREEMIUM_TRIAL_DOCGEN = 2
FREEMIUM_TRIAL_DURATION_DAYS = 30


# ============================================
# TOP-UP SYSTEM
# ============================================

TOP_UP_MIN_MINUTES = 5  # Minimum 5 minutes
TOP_UP_MAX_MINUTES = 30  # Maximum 30 minutes
TOP_UP_PRICE_PER_MINUTE = Decimal("1.00")  # £1 per minute

def calculate_topup_price(minutes: int) -> Decimal:
    """Calculate price for top-up in GBP"""
    return TOP_UP_PRICE_PER_MINUTE * minutes


# ============================================
# ALERT THRESHOLDS
# ============================================

USAGE_ALERT_THRESHOLD_PERCENT = 85  # Alert at 85% usage
TOP_UP_SUGGESTION_COUNT = 2  # Suggest upgrade after 2 top-ups in a month


# ============================================
# PROMOTIONAL RULES
# ============================================

FIRST_MONTH_DISCOUNT_PERCENT = 50  # 50% off first month for new subscribers

def get_first_month_price(plan_id: str) -> Decimal:
    """Get discounted first month price"""
    plan = SUBSCRIPTION_PLANS.get(plan_id)
    if not plan or plan["price_gbp"] == Decimal("0.00"):
        return Decimal("0.00")
    return plan["price_gbp"] * Decimal("0.50")


# ============================================
# LAWYER CONSULTATION COMMISSIONS
# ============================================

COMMISSION_RATES = {
    "in_house": Decimal("0.03"),  # 3% for in-house rates
    "custom": Decimal("0.05"),    # 5% for lawyer-custom rates
    "casework": Decimal("0.10"),  # 10% for casework
}


# ============================================
# HELPER FUNCTIONS
# ============================================

def get_plan(plan_id: str) -> Optional[Dict[str, Any]]:
    """Get plan configuration by ID"""
    return SUBSCRIPTION_PLANS.get(plan_id)


def get_tier_limits(tier: str) -> Dict[str, Any]:
    """Get service limits for a tier"""
    # Find a plan with this tier to get limits
    for plan in SUBSCRIPTION_PLANS.values():
        if plan["tier"] == tier:
            return {
                "voice_notes_limit": plan["voice_notes_limit"],
                "uploads_limit": plan["uploads_limit"],
                "docgen_limit": plan["docgen_limit"],
                "max_image_size_mb": plan["max_image_size_mb"],
                "max_doc_pages": plan["max_doc_pages"],
                "unlimited_ai_queries": plan["unlimited_ai_queries"],
            }
    return {}


def is_unlimited(limit: Optional[int]) -> bool:
    """Check if a limit is unlimited (None)"""
    return limit is None


def get_plans_by_tier(tier: str) -> Dict[str, Dict[str, Any]]:
    """Get all plans for a specific tier"""
    return {
        plan_id: plan 
        for plan_id, plan in SUBSCRIPTION_PLANS.items() 
        if plan["tier"] == tier
    }


def get_upgrade_options(current_plan_id: str) -> list:
    """Get available upgrade options from current plan"""
    current = SUBSCRIPTION_PLANS.get(current_plan_id)
    if not current:
        return []
    
    tier_order = [
        SubscriptionTier.FREEMIUM.value,
        SubscriptionTier.EXPRESS.value,
        SubscriptionTier.STANDARD.value,
        SubscriptionTier.PREMIUM.value
    ]
    current_tier_index = tier_order.index(current["tier"])
    
    upgrades = []
    for plan_id, plan in SUBSCRIPTION_PLANS.items():
        plan_tier_index = tier_order.index(plan["tier"])
        # Higher tier or same tier with higher price
        if plan_tier_index > current_tier_index or (
            plan_tier_index == current_tier_index and 
            plan["price_gbp"] > current["price_gbp"]
        ):
            upgrades.append(plan)
    
    return sorted(upgrades, key=lambda x: x["price_gbp"])


# ============================================
# ERROR MESSAGES
# ============================================

ERROR_MESSAGES = {
    BlockedReason.INSUFFICIENT_MINUTES: "You don't have enough minutes. Top up or upgrade your plan.",
    BlockedReason.MONTHLY_CAP_EXCEEDED: "You've reached your monthly limit for this service.",
    BlockedReason.FILE_SIZE_EXCEEDED: "This file exceeds the size limit. Please upload a smaller file.",
    BlockedReason.PAGE_LIMIT_EXCEEDED: "This document exceeds the page limit. Please split it into smaller documents.",
    BlockedReason.QUERY_LIMIT_EXCEEDED: "You've used all your queries for this period. Wait for reset or purchase a daily pass.",
    BlockedReason.TRIAL_EXPIRED: "Your trial period has ended. Upgrade to continue using this feature.",
    BlockedReason.SUBSCRIPTION_INACTIVE: "Your subscription is not active. Please renew or upgrade.",
    BlockedReason.DAILY_LIMIT_REACHED: "You've reached your daily limit. Try again tomorrow or upgrade.",
    BlockedReason.UPGRADE_REQUIRED: "This feature requires a higher subscription tier.",
}


def get_block_message(
    reason: BlockedReason, 
    tier: str = None,
    current_usage: int = None,
    limit: int = None
) -> str:
    """Get a user-friendly error message for a blocked action"""
    base_message = ERROR_MESSAGES.get(reason, "Action not allowed.")
    
    # Add context if available
    if current_usage is not None and limit is not None:
        base_message += f" (Used: {current_usage}/{limit})"
    
    return base_message
