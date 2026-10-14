from fastapi import APIRouter, HTTPException, BackgroundTasks
from pydantic import BaseModel, EmailStr, Field
import logging
from services.auth import Auth
from services.db import DB
from services.email import EmailService

logger = logging.getLogger(__name__)

PROFESSIONAL_USER_TYPES = {
    "lawyer",
    "law firm",
    "hr/legal (sme)",
    "a2j partners",
}

# Request models for registration email verification only
class VerifyEmailRequest(BaseModel):
    email: EmailStr
    otp: str = Field(..., min_length=6, max_length=6)

class ResendOTPRequest(BaseModel):
    email: EmailStr

class EmailVerificationAPI:
    def __init__(self, *, app_name, db: DB, auth: Auth):
        self.app_name = app_name
        self.db = db
        self.auth = auth
        
    def get_router(self):
        router = APIRouter(prefix="/email-verification", tags=["email-verification"])
        
        @router.post("/verify-registration-otp")
        async def verify_registration_otp(
            background_tasks: BackgroundTasks,
            request: VerifyEmailRequest
        ):
            """
            Verify OTP and complete user registration (for users who registered through /create_user or /create_pro_account).
            """
            try:
                # Complete the registration
                result = self.auth.complete_user_registration(request.email, request.otp)

                # Send welcome email in background now that the account exists.
                # user_type is "User" for regular accounts, or the professional_type
                # value (e.g. "lawyer", "law_firm", "hr_legal") for any professional
                # account - see Auth.complete_user_registration, line ~1690.
                user_type = result["user_type"]
                to_email = result["user"].get("email") or request.email
                first_name = result["user"].get("first_name") or "there"

                # Regular accounts historically returned ``None`` here because
                # their professional_type column is NULL. Use an allowlist so
                # null, empty, malformed, or future client values can never
                # fall through to the professional template.
                normalized_user_type = str(user_type or "").strip().lower()
                is_professional = normalized_user_type in PROFESSIONAL_USER_TYPES

                if is_professional:
                    background_tasks.add_task(
                        EmailService.send_pro_account_created_email,
                        to_email,
                        first_name
                    )
                else:
                    background_tasks.add_task(
                        EmailService.send_account_created_email,
                        to_email,
                        first_name
                    )

                return {
                    "message": "Email verified successfully! Your account has been created.",
                    "token": result["token"],
                    "user": result["user"],
                    "user_type": result["user_type"]
                }
                
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error verifying registration OTP: {str(e)}")
                raise HTTPException(status_code=500, detail="Registration verification failed")

        @router.post("/resend-registration-otp")
        async def resend_registration_otp(
            background_tasks: BackgroundTasks,
            request: ResendOTPRequest
        ):
            """
            Resend registration OTP for pending registrations.
            """
            try:
                otp_data = self.auth.resend_email_verification_token(request.email)
                background_tasks.add_task(
                    EmailService.send_email_verification_email,
                    otp_data["email"], otp_data["otp"],
                )
                return {
                    "message": "Verification code resent. Please check your email.",
                    "email": otp_data["email"],
                    "expires_in": "15 minutes",
                }

            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error resending registration OTP: {str(e)}")
                raise HTTPException(status_code=500, detail="Failed to resend verification code")

        return router
