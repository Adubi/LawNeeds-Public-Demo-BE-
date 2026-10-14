from fastapi import APIRouter, HTTPException, Form, Request, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, EmailStr
from typing import Optional, Dict, Any
import logging
from services.oauth import OAuthService
from services.db import DB
from services.auth import Auth
from services.models import User
import httpx

logger = logging.getLogger(__name__)

class OAuthCallbackRequest(BaseModel):
    code: str
    state: Optional[str] = None
    device: Optional[str] = None  # 'ios' or 'android'
    code_verifier: Optional[str] = None  # PKCE code verifier for mobile flows

class LinkAccountRequest(BaseModel):
    provider: str  # 'google' or 'apple'
    oauth_code: str
    oauth_state: Optional[str] = None

class UnlinkAccountRequest(BaseModel):
    provider: str  # 'google' or 'apple'

class OAuthAPI:
    def __init__(self, *, app_name: str, db: DB, auth: Auth):
        self.app_name = app_name
        self.db = db
        self.auth = auth
        self.oauth_service = OAuthService(db, auth)
    
    def get_router(self):
        router = APIRouter(prefix="/oauth", tags=["oauth"])
        
        @router.get("/google/authorize")
        async def google_authorize(
            state: Optional[str] = Query(None, description="Optional state parameter for CSRF protection"),
            mobile: bool = Query(False, description="Whether this is a mobile app request"),
            device: Optional[str] = Query(None, description="Device type: 'ios' or 'android' (for mobile requests)"),
            code_challenge: Optional[str] = Query(None, description="PKCE code challenge for mobile flows")
        ):
            """
            Get Google OAuth authorization URL.
            
            Returns the URL that the frontend should redirect the user to for Google authentication.
            """
            try:
                # Validate device parameter if provided
                if device and device not in ['ios', 'android']:
                    raise HTTPException(status_code=400, detail="Device must be 'ios' or 'android'")
                
                auth_url = self.oauth_service.get_google_auth_url(state, mobile=mobile, device=device, code_challenge=code_challenge)
                return {
                    "success": True,
                    "authorization_url": auth_url,
                    "provider": "google",
                    "mobile": mobile,
                    "device": device
                }
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error in Google authorize endpoint: {str(e)}")
                raise HTTPException(status_code=500, detail="Failed to generate authorization URL")
    
        @router.post("/google/callback")
        async def google_callback(request: OAuthCallbackRequest):
            """
            Handle Google OAuth callback.
            
            This endpoint processes the authorization code received from Google
            and either creates a new user or logs in an existing user.
            """
            try:
                # Validate device parameter if provided
                if request.device and request.device not in ['ios', 'android']:
                    raise HTTPException(status_code=400, detail="Device must be 'ios' or 'android'")
                
                result = await self.oauth_service.handle_google_callback(
                    code=request.code,
                    state=request.state,
                    device=request.device,
                    code_verifier=request.code_verifier
                )
                
                return {
                    "success": True,
                    "message": "Google authentication successful",
                    **result
                }
                
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error in Google callback endpoint: {str(e)}")
            raise HTTPException(status_code=500, detail="OAuth authentication failed")
    
        @router.get("/apple/authorize")
        async def apple_authorize(
            state: Optional[str] = Query(None, description="Optional state parameter for CSRF protection"),
            mobile: bool = Query(False, description="Whether this is a mobile app request"),
            device: Optional[str] = Query(None, description="Device type: 'ios' or 'android' (for mobile requests)")
        ):
            """
            Get Apple OAuth authorization URL.
            
            Returns the URL that the frontend should redirect the user to for Apple authentication.
            """
            try:
                # Validate device parameter if provided
                if device and device not in ['ios', 'android']:
                    raise HTTPException(status_code=400, detail="Device must be 'ios' or 'android'")
                
                auth_url = self.oauth_service.get_apple_auth_url(state, mobile=mobile, device=device)
                return {
                    "success": True,
                    "authorization_url": auth_url,
                    "provider": "apple",
                    "mobile": mobile,
                    "device": device
                }
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error in Apple authorize endpoint: {str(e)}")
                raise HTTPException(status_code=500, detail="Failed to generate authorization URL")
        
        @router.post("/apple/callback")
        async def apple_callback(
            code: Optional[str] = Form(None, description="Authorization code from Apple"),
            id_token: Optional[str] = Form(None, description="ID token from Apple (web flow)"),
            identity_token: Optional[str] = Form(None, description="Identity token from Apple (mobile flow)"),
            state: Optional[str] = Form(None, description="State parameter"),
            user: Optional[str] = Form(None, description="User data from Apple")
        ):
            """
            Handle Apple OAuth callback for both web and mobile flows.
            """
            try:
                # Log what Apple sent us
                logger.info(f"Apple callback received - code: {code[:20] if code else 'None'}..., "
                           f"id_token: {'Present' if id_token else 'None'}, "
                           f"identity_token: {'Present' if identity_token else 'None'}, "
                           f"user: {user if user else 'None'}")
                
                # Use either id_token (web) or identity_token (mobile)
                token = id_token or identity_token
                
                if not token:
                    raise HTTPException(status_code=400, detail="Either id_token or identity_token is required")
                
                result = await self.oauth_service.handle_apple_callback(
                    code=code or "",
                    id_token=token,
                    state=state,
                    user_data=user
                )
                
                return {
                    "success": True,
                    "message": "Apple authentication successful",
                    **result
                }
                
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error in Apple callback endpoint: {str(e)}")
                raise HTTPException(status_code=500, detail="OAuth authentication failed")
    
        @router.post("/link-account")
        async def link_oauth_account(
            request: Request,
            link_request: LinkAccountRequest
        ):
            """
            Link an OAuth account to an existing authenticated user.
            
            This allows users who already have an account to link their
            Google or Apple account for easier sign-in.
            """
            try:
                # Verify the user is authenticated
                token_data = self.auth.verify_request_token(request)
                user_id = token_data.get('user_id')
                
                if not user_id:
                    raise HTTPException(status_code=401, detail="Authentication required")
                
                # Validate provider
                if link_request.provider not in ['google', 'apple']:
                    raise HTTPException(status_code=400, detail="Invalid OAuth provider")
                
                # Handle OAuth callback to get OAuth user data
                if link_request.provider == 'google':
                    oauth_result = await self.oauth_service.handle_google_callback(
                        link_request.oauth_code, 
                        link_request.oauth_state
                    )
                elif link_request.provider == 'apple':
                    raise HTTPException(
                        status_code=400, 
                        detail="Apple account linking requires special handling. Please use the Apple callback endpoint."
                    )
                
                # Extract OAuth information
                oauth_user = oauth_result.get('user', {})
                oauth_id = oauth_user.get('id')
                oauth_email = oauth_user.get('email')
                
                # Link the accounts
                success = self.oauth_service.link_oauth_to_user(
                    user_id, 
                    link_request.provider, 
                    oauth_id, 
                    oauth_email
                )
                
                if success:
                    return {
                        "success": True,
                        "message": f"Successfully linked {link_request.provider.title()} account",
                        "provider": link_request.provider
                    }
                else:
                    raise HTTPException(status_code=500, detail="Failed to link account")
                
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error linking OAuth account: {str(e)}")
                raise HTTPException(status_code=500, detail="Failed to link account")
        
        @router.post("/unlink-account")
        async def unlink_oauth_account(
            request: Request,
            unlink_request: UnlinkAccountRequest
        ):
            """
            Unlink an OAuth account from the current user.
            
            This removes the OAuth connection, but the user can still
            sign in with their password (if they have one set).
            """
            try:
                # Verify the user is authenticated
                token_data = self.auth.verify_request_token(request)
                user_id = token_data.get('user_id')
                
                if not user_id:
                    raise HTTPException(status_code=401, detail="Authentication required")
                
                # Validate provider
                if unlink_request.provider not in ['google', 'apple']:
                    raise HTTPException(status_code=400, detail="Invalid OAuth provider")
                
                # Unlink the OAuth account
                success = self.oauth_service.unlink_oauth_from_user(user_id, unlink_request.provider)
                
                if success:
                    return {
                        "success": True,
                        "message": f"Successfully unlinked {unlink_request.provider.title()} account",
                        "provider": unlink_request.provider
                    }
                else:
                    raise HTTPException(status_code=500, detail="Failed to unlink account")
                
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error unlinking OAuth account: {str(e)}")
                raise HTTPException(status_code=500, detail="Failed to unlink account")
        
        @router.get("/user/oauth-status")
        async def get_user_oauth_status(request: Request):
            """
            Get the current user's OAuth connection status.
            
            Returns information about which OAuth providers are linked
            to the current user's account.
            """
            try:
                # Verify the user is authenticated
                token_data = self.auth.verify_request_token(request)
                user_id = token_data.get('user_id')
                
                if not user_id:
                    raise HTTPException(status_code=401, detail="Authentication required")
                
                with self.db.get_session() as session:
                    user = session.query(User).filter(User.id == user_id).first()
                    if not user:
                        raise HTTPException(status_code=404, detail="User not found")
                
                return {
                        "success": True,
                        "oauth_provider": user.oauth_provider,
                        "has_password": bool(user.hashed_password),
                        "is_email_verified": user.is_email_verified,
                        "can_unlink_oauth": bool(user.hashed_password) if user.oauth_provider else False
                }
                
            except HTTPException:
                raise
            except Exception as e:
                    logger.error(f"Error getting OAuth status: {str(e)}")
                    raise HTTPException(status_code=500, detail="Failed to get OAuth status")
        
        @router.get("/config")
        async def get_oauth_config():
            """
            Get OAuth configuration for the frontend.
            
            Returns the client IDs and redirect URIs that the frontend
            needs to initiate OAuth flows for both web and mobile.
            """
            try:
                import os
                
                return {
                    "success": True,
                    "google": {
                        "client_id": os.getenv('GOOGLE_OAUTH_CLIENT_ID'),  # Web client ID
                        "ios_client_id": os.getenv('GOOGLE_OAUTH_IOS_CLIENT_ID'),
                        "android_client_id": os.getenv('GOOGLE_OAUTH_ANDROID_CLIENT_ID'),
                        "web_redirect_uri": os.getenv('GOOGLE_OAUTH_REDIRECT_URI'),
                        "ios_redirect_uri": os.getenv('GOOGLE_OAUTH_IOS_REDIRECT_URI'),
                        "android_redirect_uri": os.getenv('GOOGLE_OAUTH_ANDROID_REDIRECT_URI')
                    },
                    "apple": {
                        "client_id": os.getenv('APPLE_OAUTH_CLIENT_ID'),
                        "web_redirect_uri": os.getenv('APPLE_OAUTH_REDIRECT_URI', 'http://localhost:3000/auth/apple/callback'),
                        "mobile_redirect_uri": os.getenv('APPLE_OAUTH_MOBILE_REDIRECT_URI', 'com.lawneeds.app://oauth/apple/callback')
                    }
                }
                
            except Exception as e:
                logger.error(f"Error getting OAuth config: {str(e)}")
                raise HTTPException(status_code=500, detail="Failed to get OAuth configuration")
        
        @router.get("/mobile/callback")
        async def mobile_oauth_callback(
            code: str = Query(..., description="Authorization code from OAuth provider"),
            state: Optional[str] = Query(None, description="State parameter"),
            provider: str = Query(..., description="OAuth provider (google or apple)"),
            device: Optional[str] = Query(None, description="Device type: 'ios' or 'android'"),
            code_verifier: Optional[str] = Query(None, description="PKCE code verifier")
        ):
            """
            Handle mobile OAuth callback for both Google and Apple.
            
            This endpoint handles the deep link callback from mobile OAuth flows.
            """
            try:
                # Validate device parameter if provided
                if device and device not in ['ios', 'android']:
                    raise HTTPException(status_code=400, detail="Device must be 'ios' or 'android'")
                
                if provider == "google":
                    result = await self.oauth_service.handle_google_callback(code=code, state=state, device=device, code_verifier=code_verifier)
                elif provider == "apple":
                    # For Apple mobile, we'll get the code via query params instead of form post
                    # You might need to get id_token differently for mobile Apple OAuth
                    raise HTTPException(status_code=400, detail="Apple mobile OAuth requires additional implementation")
                else:
                    raise HTTPException(status_code=400, detail="Invalid OAuth provider")
                
                return {
                    "success": True,
                    "message": f"{provider.title()} authentication successful",
                    **result
                }
                
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error in mobile OAuth callback: {str(e)}")
                raise HTTPException(status_code=500, detail="OAuth authentication failed")
        
        return router