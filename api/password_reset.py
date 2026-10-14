from fastapi import APIRouter, HTTPException, Depends, Body
from pydantic import BaseModel, EmailStr, Field
import logging
from services.auth import Auth
from services.db import DB
from services.email import EmailService
from typing import Optional

logger = logging.getLogger(__name__)

# Request password reset model
class PasswordResetRequest(BaseModel):
    email: EmailStr

# Verify OTP model
class VerifyOTPRequest(BaseModel):
    email: EmailStr
    otp: str = Field(..., min_length=6, max_length=6, pattern=r'^\d{6}$')

# Set new password model with OTP
class SetNewPasswordRequest(BaseModel):
    email: EmailStr
    otp: str = Field(..., min_length=6, max_length=6, pattern=r'^\d{6}$')
    password: str = Field(..., min_length=8)
    password_confirm: str

# Verify OTP response model
class VerifyOTPResponse(BaseModel):
    valid: bool
    email: Optional[str] = None
    message: Optional[str] = None

class PasswordResetAPI:
    def __init__(self, *, app_name, db: DB, auth: Auth):
        self.app_name = app_name
        self.db = db
        self.auth = auth
        
    def get_router(self):
        router = APIRouter(prefix="/password", tags=["password"])
        
        @router.post("/forgot")
        async def request_password_reset(request: PasswordResetRequest):
            """
            Request a password reset OTP to be sent to the user's email.
            """
            try:
                # Create password reset OTP
                otp_data = self.auth.create_password_reset_token(request.email)
                
                if not otp_data:
                    # Don't reveal if email exists or not (security best practice)
                    return {"message": "If the email exists, a password reset code will be sent."}
                
                # Send password reset email with OTP
                email_sent = EmailService.send_password_reset_email(
                    request.email, 
                    otp_data["otp"]
                )
                
                if not email_sent:
                    logger.error(f"Failed to send password reset email to {request.email}")
                    # Don't reveal email sending failure to user
                    
                return {"message": "If the email exists, a password reset code will be sent."}
                
            except Exception as e:
                logger.error(f"Error in password reset request: {str(e)}")
                raise HTTPException(status_code=500, detail="Failed to process password reset request")
        
        @router.post("/verify-otp")
        async def verify_otp(request: VerifyOTPRequest):
            """
            Verify if a password reset OTP is valid.
            """
            try:
                # Verify the OTP
                verification_result = self.auth.verify_password_reset_otp(request.email, request.otp)
                
                # If we get here, OTP is valid
                return VerifyOTPResponse(
                    valid=True,
                    email=verification_result.get("email"),
                    message="OTP verified successfully"
                )
                
            except HTTPException as e:
                # Return invalid but don't throw an exception
                logger.warning(f"Invalid password reset OTP for {request.email}: {str(e)}")
                return VerifyOTPResponse(
                    valid=False,
                    message="Invalid or expired OTP"
                )
            except Exception as e:
                logger.error(f"Error verifying password reset OTP: {str(e)}")
                return VerifyOTPResponse(
                    valid=False,
                    message="Failed to verify OTP"
                )
        
        @router.post("/reset")
        async def reset_password(request: SetNewPasswordRequest):
            """
            Reset the user's password using the OTP from the email.
            """
            try:
                # Check if passwords match
                if request.password != request.password_confirm:
                    raise HTTPException(
                        status_code=400,
                        detail="Passwords do not match"
                    )
                
                # Reset the password using OTP
                success = self.auth.reset_password_with_otp(
                    request.email, 
                    request.otp, 
                    request.password
                )
                
                if success:
                    return {"message": "Password has been reset successfully"}
                else:
                    raise HTTPException(
                        status_code=400,
                        detail="Failed to reset password"
                    )
                    
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error resetting password: {str(e)}")
                raise HTTPException(status_code=500, detail="Failed to reset password")
                
        return router 