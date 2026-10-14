# services/revenuecat_service.py
"""
RevenueCat Webhook Service for LawNeeds
Validates webhook requests and normalizes RevenueCat event payloads.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional, Tuple
from config.environment import (
    get_payments_env,
    get_revenuecat_webhook_secret,
    load_environment,
)

logger = logging.getLogger(__name__)


class RevenueCatService:
    """
    Helper for RevenueCat webhook verification and event normalization.
    """

    def __init__(self) -> None:
        load_environment()
        self.payments_env = get_payments_env()
        self.webhook_secret = get_revenuecat_webhook_secret()

    def verify_webhook(self, headers: Mapping[str, str]) -> Tuple[bool, str]:
        """
        Verify webhook request using a shared secret header.

        RevenueCat supports a configured secret sent in the Authorization header.
        Accepts either "Bearer <secret>" or raw secret values.
        """
        if not self.webhook_secret:
            return False, f"RevenueCat webhook secret not configured for {self.payments_env}"

        header_value = self._get_auth_header(headers)
        if not header_value:
            return False, "Missing authorization header"

        token = header_value
        if header_value.lower().startswith("bearer "):
            token = header_value.split(" ", 1)[1].strip()

        if token != self.webhook_secret:
            return False, "Invalid webhook secret"

        return True, "OK"

    def parse_event(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Normalize RevenueCat webhook payload into a flat event structure.
        """
        event = payload.get("event") if isinstance(payload.get("event"), dict) else payload

        event_type = event.get("type")
        event_id = event.get("id") or event.get("event_id")
        app_user_id = event.get("app_user_id")
        
        # user_id is our actual user UUID (may be passed separately from app_user_id)
        # This handles cases where app_user_id is RevenueCat's anonymous ID format
        user_id = event.get("user_id")

        product_id = (
            event.get("product_id")
            or event.get("product_identifier")
            or event.get("product_id")
        )

        entitlement_ids = (
            event.get("entitlement_ids")
            or event.get("entitlements")
            or event.get("entitlement_id")
            or []
        )
        if isinstance(entitlement_ids, str):
            entitlement_ids = [entitlement_ids]
        elif isinstance(entitlement_ids, dict):
            entitlement_ids = list(entitlement_ids.keys())
        elif entitlement_ids is None:
            entitlement_ids = []

        expiration_at = self._to_datetime(
            event.get("expiration_at_ms")
            or event.get("expires_at_ms")
            or event.get("expiration_at")
        )
        purchased_at = self._to_datetime(
            event.get("purchased_at_ms")
            or event.get("purchase_date_ms")
            or event.get("purchased_at")
        )

        return {
            "event_id": event_id,
            "type": event_type,
            "user_id": user_id,  # Our actual user UUID
            "app_user_id": app_user_id,  # RevenueCat's app_user_id (may be anonymous)
            "product_id": product_id,
            "entitlement_ids": entitlement_ids,
            "expiration_at": expiration_at,
            "purchased_at": purchased_at,
            "original_transaction_id": event.get("original_transaction_id"),
            "transaction_id": event.get("transaction_id"),
            "environment": event.get("environment"),
            "store": event.get("store"),
            "raw_event": event,
        }

    def _get_auth_header(self, headers: Mapping[str, str]) -> Optional[str]:
        for key in ("authorization", "x-authorization", "x-revenuecat-authorization"):
            value = headers.get(key)
            if value:
                return value
        return None

    def _to_datetime(self, value: Any) -> Optional[datetime]:
        if value is None:
            return None
        try:
            if isinstance(value, (int, float)):
                return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
            if isinstance(value, str):
                if value.isdigit():
                    return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)
                iso_value = value.replace("Z", "+00:00")
                parsed = datetime.fromisoformat(iso_value)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return parsed
        except (ValueError, OSError, OverflowError) as exc:
            logger.warning(f"Invalid timestamp value from RevenueCat: {value} ({exc})")
        return None
