import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import requests
from fastapi import HTTPException
from sqlalchemy import select

from services.models import IdentityVerification, PersonaWebhookEvent, User, DeviceToken
from services.email import EmailService
from services.notification_service import NotificationService
from services.firebase_notification_service import FirebaseNotificationService


logger = logging.getLogger(__name__)


ACCOUNT_TYPE_BY_PROFESSIONAL_TYPE = {
    None: "user",
    "Lawyer": "lawyer",
    "Law Firm": "law_firm",
    "A2J Partners": "a2j_partner",
    "SME": "sme",
    "HR/Legal (SME)": "sme",
}

TEMPLATE_ENV_BY_ACCOUNT_TYPE = {
    "user": "PERSONA_TEMPLATE_ID",
    "lawyer": "PERSONA_TEMPLATE_ID_LAWYER",
    "law_firm": "PERSONA_TEMPLATE_ID_LAW_FIRM",
    "a2j_partner": "PERSONA_TEMPLATE_ID_A2J",
    "sme": "PERSONA_TEMPLATE_ID_SME",
}

ELIGIBLE_PROFESSIONAL_TYPES = set(ACCOUNT_TYPE_BY_PROFESSIONAL_TYPE.keys())
RESUMABLE_STATUSES = {"not_started", "in_progress", "expired"}
PERSONA_STATUS_MAP = {
    "created": "not_started",
    "pending": "in_progress",
    "completed": "pending_review",
    "approved": "approved",
    "declined": "declined",
    "failed": "failed",
    "expired": "expired",
    "needs_review": "pending_review",
    "marked_for_review": "pending_review",
}


def identity_verification_required(user: User) -> bool:
    return user.professional_type in ELIGIBLE_PROFESSIONAL_TYPES


def identity_account_type(user: User) -> str:
    return ACCOUNT_TYPE_BY_PROFESSIONAL_TYPE.get(user.professional_type, "user")


def identity_template_id_for_user(user: User) -> Optional[str]:
    account_type = identity_account_type(user)
    template_env = TEMPLATE_ENV_BY_ACCOUNT_TYPE.get(account_type)
    template_id = os.getenv(template_env) if template_env else None
    return template_id or os.getenv("PERSONA_TEMPLATE_ID")


def latest_identity_verification(session, user_id) -> Optional[IdentityVerification]:
    return (
        session.query(IdentityVerification)
        .filter(IdentityVerification.user_id == user_id)
        .order_by(IdentityVerification.created_at.desc())
        .first()
    )


def identity_verification_approved(session, user_id) -> bool:
    """Return whether the user's most recent Persona inquiry was approved."""
    verification = latest_identity_verification(session, user_id)
    return bool(verification and verification.status == "approved")


def latest_persona_verification_approved_clause(user_id_column):
    """Build a local SQL predicate for a user's latest Persona inquiry.

    This intentionally reads only ``identity_verifications``.  Discovery
    endpoints must never call Persona per result (or per page request); the
    signed Persona webhook keeps this local status current instead.
    """
    latest_status = (
        select(IdentityVerification.status)
        .where(
            IdentityVerification.user_id == user_id_column,
            IdentityVerification.provider == "persona",
        )
        .order_by(IdentityVerification.created_at.desc())
        .limit(1)
        .scalar_subquery()
    )
    return latest_status == "approved"


def persona_enforcement_required() -> bool:
    return os.getenv("PERSONA_ENFORCEMENT_MODE", "prompt").lower() == "required"


def persona_is_configured() -> bool:
    return bool(
        os.getenv("PERSONA_ENABLED", "true").lower() == "true"
        and os.getenv("PERSONA_API_KEY")
        and os.getenv("PERSONA_WEBHOOK_SECRET")
        and os.getenv("PERSONA_TEMPLATE_ID")
    )


def persona_is_configured_for_user(user: User) -> bool:
    return bool(
        os.getenv("PERSONA_ENABLED", "true").lower() == "true"
        and os.getenv("PERSONA_API_KEY")
        and os.getenv("PERSONA_WEBHOOK_SECRET")
        and identity_template_id_for_user(user)
    )


def require_identity_verification(session, user: User) -> None:
    if not persona_enforcement_required():
        return
    if identity_verification_required(user) and not identity_verification_approved(session, user.id):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "identity_verification_required",
                "message": "Complete identity verification to continue.",
            },
        )


def require_identity_verification_for_feature(session, user: User) -> None:
    if (
        persona_is_configured_for_user(user)
        and identity_verification_required(user)
        and not identity_verification_approved(session, user.id)
    ):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "identity_verification_required",
                "message": "Complete identity verification to use this feature.",
            },
        )


class PersonaService:
    API_BASE_URL = "https://api.withpersona.com/api/v1"

    def __init__(self):
        self.api_key = os.getenv("PERSONA_API_KEY")
        self.webhook_secret = os.getenv("PERSONA_WEBHOOK_SECRET")
        self.template_id = os.getenv("PERSONA_TEMPLATE_ID")
        self.environment = os.getenv("PERSONA_ENVIRONMENT", "sandbox").lower()
        self.enabled = os.getenv("PERSONA_ENABLED", "true").lower() == "true"
        self._firebase_service: Optional[FirebaseNotificationService] = None

    def is_configured(self) -> bool:
        return bool(self.enabled and self.api_key and self.webhook_secret and self.template_id)

    def is_configured_for_user(self, user: User) -> bool:
        return bool(self.enabled and self.api_key and self.webhook_secret and self.template_id_for_user(user))

    def template_id_for_user(self, user: User) -> Optional[str]:
        return identity_template_id_for_user(user)

    def status_payload(self, session, user: User) -> Dict[str, Any]:
        eligible = identity_verification_required(user)
        configured = self.is_configured_for_user(user) if eligible else self.is_configured()
        verification = latest_identity_verification(session, user.id) if eligible else None
        status = verification.status if verification else ("not_started" if eligible else "not_required")
        enforcement_mode = os.getenv("PERSONA_ENFORCEMENT_MODE", "prompt").lower()
        return {
            "enabled": self.enabled,
            "configured": configured,
            "eligible": eligible,
            "required": eligible and configured,
            "enforcement_mode": enforcement_mode,
            "blocking": eligible and configured and enforcement_mode == "required",
            "account_type": identity_account_type(user) if eligible else None,
            "status": status,
            "approved": status == "approved",
            "inquiry_id": verification.inquiry_id if verification else None,
            "provider_status": verification.provider_status if verification else None,
        }

    async def start_or_resume(self, session, user: User) -> Dict[str, Any]:
        if not identity_verification_required(user):
            raise HTTPException(status_code=403, detail="Identity verification is not required for this account type.")
        template_id = self.template_id_for_user(user)
        if not self.is_configured_for_user(user):
            raise HTTPException(status_code=503, detail="Persona identity verification is not configured.")

        verification = latest_identity_verification(session, user.id)
        if verification and verification.status == "approved":
            return {**self.status_payload(session, user), "session_token": None}
        if verification and verification.status in {"pending_review"}:
            return {**self.status_payload(session, user), "session_token": None}

        if verification and verification.status in RESUMABLE_STATUSES:
            response = self._request("POST", f"/inquiries/{verification.inquiry_id}/resume")
            await self._apply_inquiry_response(session, verification, response)
        else:
            response = self._request(
                "POST",
                "/inquiries",
                json_body={
                    "data": {
                        "attributes": {
                            "inquiry-template-id": template_id,
                            "reference-id": str(user.id),
                            "note": f"LawNeeds {identity_account_type(user)} identity verification",
                        }
                    }
                },
                idempotency_key=str(uuid.uuid4()),
            )
            data = response["data"]
            attributes = data.get("attributes", {})
            verification = IdentityVerification(
                user_id=user.id,
                inquiry_id=data["id"],
                template_id=template_id,
                environment=self.environment,
                account_type=identity_account_type(user),
                status=self.map_status(attributes.get("status")),
                provider_status=attributes.get("status"),
            )
            session.add(verification)

        session.flush()
        return {
            **self.status_payload(session, user),
            "inquiry_id": verification.inquiry_id,
            "session_token": response.get("meta", {}).get("session-token"),
        }

    async def sync(self, session, user: User) -> Dict[str, Any]:
        verification = latest_identity_verification(session, user.id)
        if not verification:
            return self.status_payload(session, user)
        response = self._request("GET", f"/inquiries/{verification.inquiry_id}")
        await self._apply_inquiry_response(session, verification, response)
        session.flush()
        return self.status_payload(session, user)

    async def process_webhook(self, session, raw_body: bytes, signature_header: str) -> Dict[str, Any]:
        if not self.verify_webhook_signature(raw_body, signature_header):
            raise HTTPException(status_code=401, detail="Invalid Persona webhook signature")

        payload = json.loads(raw_body.decode("utf-8"))
        event_data = payload.get("data", {})
        event_id = event_data.get("id")
        attributes = event_data.get("attributes", {})
        event_name = attributes.get("name", "")
        inquiry_data = attributes.get("payload", {}).get("data", {})
        inquiry_id = inquiry_data.get("id") if inquiry_data.get("type") == "inquiry" else None

        if not event_id:
            raise HTTPException(status_code=400, detail="Persona webhook event ID is missing")
        if session.query(PersonaWebhookEvent).filter(PersonaWebhookEvent.event_id == event_id).first():
            return {"processed": False, "duplicate": True}

        event_created_at = self.parse_datetime(attributes.get("created-at"))
        session.add(
            PersonaWebhookEvent(
                event_id=event_id,
                event_name=event_name or "unknown",
                inquiry_id=inquiry_id,
                event_created_at=event_created_at,
                payload=payload,
            )
        )

        if inquiry_id:
            verification = (
                session.query(IdentityVerification)
                .filter(IdentityVerification.inquiry_id == inquiry_id)
                .first()
            )
            if verification and (
                not verification.last_event_at
                or not event_created_at
                or event_created_at >= self.ensure_aware(verification.last_event_at)
            ):
                inquiry_attributes = inquiry_data.get("attributes", {})
                provider_status = inquiry_attributes.get("status") or event_name.removeprefix("inquiry.")
                await self._apply_status(verification, provider_status, event_created_at, session=session)

        return {"processed": True, "duplicate": False}

    def verify_webhook_signature(self, raw_body: bytes, signature_header: str) -> bool:
        if not self.webhook_secret or not signature_header:
            return False
        try:
            pairs = signature_header.split()
            timestamp = pairs[0].split(",", 1)[0].split("=", 1)[1]
            signatures = []
            for pair in pairs:
                for component in pair.split(","):
                    if component.startswith("v1="):
                        signatures.append(component.split("=", 1)[1])
            if abs(time.time() - int(timestamp)) > 300:
                return False
            signed_payload = timestamp.encode("utf-8") + b"." + raw_body
            expected = hmac.new(
                self.webhook_secret.encode("utf-8"),
                signed_payload,
                hashlib.sha256,
            ).hexdigest()
            return any(hmac.compare_digest(expected, signature) for signature in signatures)
        except (IndexError, ValueError, TypeError):
            return False

    def _request(
        self,
        method: str,
        path: str,
        json_body: Optional[Dict[str, Any]] = None,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Key-Inflection": "kebab",
        }
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        try:
            response = requests.request(
                method,
                f"{self.API_BASE_URL}{path}",
                headers=headers,
                json=json_body,
                timeout=20,
            )
            response.raise_for_status()
            return response.json()
        except requests.RequestException as exc:
            logger.error("Persona API request failed: %s", exc)
            raise HTTPException(status_code=502, detail="Persona identity verification service is unavailable")

    async def _apply_inquiry_response(
        self, session, verification: IdentityVerification, response: Dict[str, Any]
    ) -> None:
        provider_status = response.get("data", {}).get("attributes", {}).get("status")
        await self._apply_status(verification, provider_status, datetime.now(timezone.utc), session=session)

    def _get_active_device_tokens(self, session, user_id) -> list:
        rows = (
            session.query(DeviceToken)
            .filter(
                DeviceToken.user_id == user_id,
                DeviceToken.is_active == True,
            )
            .all()
        )
        return [row.device_token for row in rows]

    def _firebase(self) -> FirebaseNotificationService:
        if self._firebase_service is None:
            self._firebase_service = FirebaseNotificationService()
        return self._firebase_service

    async def _apply_status(
        self,
        verification: IdentityVerification,
        provider_status: Optional[str],
        event_created_at: Optional[datetime],
        session=None,
    ) -> None:
        # Captured before we overwrite verification.status below, so we can
        # tell an *actual transition* into declined/failed apart from a
        # duplicate/replayed webhook that re-delivers the same status.
        previous_status = verification.status

        verification.provider_status = provider_status
        verification.status = self.map_status(provider_status)
        verification.last_event_at = event_created_at or datetime.now(timezone.utc)
        now = datetime.now(timezone.utc)

        newly_approved = verification.status == "approved" and not verification.approved_at
        if newly_approved:
            verification.approved_at = now

        newly_declined_or_failed = (
            verification.status in {"declined", "failed"}
            and previous_status not in {"declined", "failed"}
        )
        if verification.status == "declined" and not verification.declined_at:
            verification.declined_at = now

        # Fire the "identity verified" email exactly once, the moment the
        # verification first transitions into "approved". Guarded by
        # `newly_approved` (checked before approved_at is set above) so
        # repeat webhook events or a later sync() call don't re-send it.
        # Requires `session` to look up the user's email - skip silently
        # if it wasn't passed in (defensive; both call sites in this file
        # do pass it).
        
        
        if newly_approved and session is not None:
            user = session.query(User).filter(User.id == verification.user_id).first()
            if user and getattr(user, "email", None):
                try:
                    if identity_account_type(user) == "user":
                        EmailService.send_identity_verified_email(
                            user.email, getattr(user, "first_name", None)
                        )
                    else:
                        EmailService.send_identity_verified_professional_email(
                            user.email, getattr(user, "first_name", None)
                        )
                except Exception:
                    # Never let an email failure break the verification flow.
                    logger.exception(
                        "Failed to send identity-verified email for user_id=%s",
                        verification.user_id,
                    )
            else:
                logger.warning(
                    "Could not send identity-verified email: no user/email for user_id=%s",
                    verification.user_id,
                )

            # In-app "verification_completed" notification, shared across
            # account types (no branching on account_type needed here).
            NotificationService(session).create(
                recipient_id=verification.user_id,
                title="Identity Verified",
                body="Your identity has been successfully verified.",
                notification_type="verification_completed",
                data={"screen": "identity-verification"},
            )

            # Push "verification_completed" notification.
            tokens = self._get_active_device_tokens(session, verification.user_id)
            if tokens:
                try:
                    await self._firebase().send_identity_verification_completed_notification(
                        device_tokens=tokens,
                        user_id=str(verification.user_id),
                    )
                except Exception:
                    logger.exception(
                        "Failed to send verification_completed push for user_id=%s",
                        verification.user_id,
                    )
                    
                    
        # In-app + push "verification_resubmission_required" notification,
        # fired once on the actual transition into declined/failed (see
        # `previous_status` guard above) rather than on every webhook that
        # re-reports an already-declined/failed inquiry.
        if newly_declined_or_failed and session is not None:
            NotificationService(session).create(
                recipient_id=verification.user_id,
                title="Action Needed: Identity Verification",
                body="We weren't able to verify your identity. Please resubmit your documents.",
                notification_type="verification_resubmission_required",
                data={"screen": "identity-verification"},
            )

            tokens = self._get_active_device_tokens(session, verification.user_id)
            if tokens:
                try:
                    await self._firebase().send_identity_verification_resubmission_notification(
                        device_tokens=tokens,
                        user_id=str(verification.user_id),
                    )
                except Exception:
                    logger.exception(
                        "Failed to send verification_resubmission_required push for user_id=%s",
                        verification.user_id,
                    )

    @staticmethod
    def map_status(provider_status: Optional[str]) -> str:
        normalized = (provider_status or "").lower().replace("-", "_")
        return PERSONA_STATUS_MAP.get(normalized, "in_progress")

    @staticmethod
    def parse_datetime(value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None

    @staticmethod
    def ensure_aware(value: datetime) -> datetime:
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
