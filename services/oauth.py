import os
import jwt
import httpx
import logging
from typing import Optional, Dict, Any
from fastapi import HTTPException
from datetime import datetime, timezone, timedelta
from .models import User
from .db import DB
from .auth import Auth
from dotenv import load_dotenv
import json
import re
import uuid
from urllib.parse import urlencode

load_dotenv()
logger = logging.getLogger(__name__)

APPLE_DEFAULT_FIRST_NAME = ""
APPLE_DEFAULT_LAST_NAME = ""

class OAuthService:
    def __init__(self, db: DB, auth: Auth):
        self.db = db
        self.auth = auth
        
        # Google OAuth Configuration
        self.google_client_id = os.getenv('GOOGLE_OAUTH_CLIENT_ID')  # Web client ID (deprecated)
        self.google_client_secret = os.getenv('GOOGLE_OAUTH_CLIENT_SECRET')
        self.google_redirect_uri = os.getenv('GOOGLE_OAUTH_REDIRECT_URI')
        self.google_mobile_redirect_uri = os.getenv('GOOGLE_OAUTH_MOBILE_REDIRECT_URI')
        
        # Device-specific Google OAuth Configuration
        # Note: iOS and Android apps don't use client secrets (public clients)
        self.google_ios_client_id = os.getenv('GOOGLE_OAUTH_IOS_CLIENT_ID')
        self.google_android_client_id = os.getenv('GOOGLE_OAUTH_ANDROID_CLIENT_ID')
        
        # Device-specific redirect URIs (Google's standardized format)
        self.google_ios_redirect_uri = os.getenv('GOOGLE_OAUTH_IOS_REDIRECT_URI')
        self.google_android_redirect_uri = os.getenv('GOOGLE_OAUTH_ANDROID_REDIRECT_URI')
        
        # Apple OAuth Configuration  
        self.apple_client_id = os.getenv('APPLE_OAUTH_CLIENT_ID')
        self.apple_team_id = os.getenv('APPLE_TEAM_ID')
        self.apple_key_id = os.getenv('APPLE_KEY_ID')
        self.apple_private_key = os.getenv('APPLE_PRIVATE_KEY')
        self.apple_redirect_uri = os.getenv('APPLE_OAUTH_REDIRECT_URI')
        self.apple_mobile_redirect_uri = os.getenv('APPLE_OAUTH_MOBILE_REDIRECT_URI')
        
        # OAuth URLs
        self.google_auth_url = 'https://accounts.google.com/o/oauth2/auth'
        self.google_token_url = 'https://oauth2.googleapis.com/token'
        self.google_userinfo_url = 'https://www.googleapis.com/oauth2/v2/userinfo'
        
        self.apple_auth_url = 'https://appleid.apple.com/auth/authorize'
        self.apple_token_url = 'https://appleid.apple.com/auth/token'

    def get_google_auth_url(self, state: Optional[str] = None, mobile: bool = False, device: Optional[str] = None, code_challenge: Optional[str] = None) -> str:
        """Generate Google OAuth authorization URL"""
        try:
            # Choose client ID and redirect URI based on device type
            if device == 'ios':
                if not self.google_ios_client_id or not self.google_ios_redirect_uri:
                    raise HTTPException(status_code=500, detail="iOS OAuth configuration is missing")
                client_id = self.google_ios_client_id
                redirect_uri = self.google_ios_redirect_uri
            elif device == 'android':
                if not self.google_android_client_id or not self.google_android_redirect_uri:
                    raise HTTPException(status_code=500, detail="Android OAuth configuration is missing")
                client_id = self.google_android_client_id
                redirect_uri = self.google_android_redirect_uri
            else:
                # Web client
                client_id = self.google_client_id
                redirect_uri = self.google_redirect_uri
                
            params = {
                'client_id': client_id,
                'redirect_uri': redirect_uri,
                'scope': 'openid email profile',
                'response_type': 'code',
                'access_type': 'offline',
                'prompt': 'consent'
            }
            
            if state:
                params['state'] = state
            
            # Add PKCE parameters for mobile flows
            if code_challenge:
                params['code_challenge'] = code_challenge
                params['code_challenge_method'] = 'S256'  # SHA256
            
            from urllib.parse import urlencode
            query_string = urlencode(params)
            return f"{self.google_auth_url}?{query_string}"
            
        except Exception as e:
            logger.error(f"Error generating Google auth URL: {str(e)}")
            raise HTTPException(status_code=500, detail="Failed to generate authorization URL")

    def get_apple_auth_url(self, state: Optional[str] = None, mobile: bool = False, device: Optional[str] = None) -> str:
        """Generate Apple OAuth authorization URL"""
        try:
            # Choose redirect URI based on device type
            if device in ['ios', 'android'] or mobile:
                redirect_uri = self.apple_mobile_redirect_uri
            else:
                redirect_uri = self.apple_redirect_uri
                
            params = {
                'client_id': self.apple_client_id,
                'redirect_uri': redirect_uri,
                'scope': 'name email',
                'response_type': 'code id_token',  # Request both code AND id_token
                'response_mode': 'form_post'
            }
            
            if state:
                params['state'] = state
                
            from urllib.parse import urlencode
            query_string = urlencode(params)
            return f"{self.apple_auth_url}?{query_string}"
            
        except Exception as e:
            logger.error(f"Error generating Apple auth URL: {str(e)}")
            raise HTTPException(status_code=500, detail="Failed to generate authorization URL")

    async def handle_google_callback(self, code: str, state: Optional[str] = None, device: Optional[str] = None, code_verifier: Optional[str] = None) -> Dict[str, Any]:
        """Handle Google OAuth callback and create/login user"""
        try:
            # Choose client credentials based on device type
            if device == 'ios':
                if not self.google_ios_client_id or not self.google_ios_redirect_uri:
                    raise HTTPException(status_code=500, detail="iOS OAuth configuration is missing")
                client_id = self.google_ios_client_id
                client_secret = None  # iOS apps don't use client secrets
                redirect_uri = self.google_ios_redirect_uri
            elif device == 'android':
                if not self.google_android_client_id or not self.google_android_redirect_uri:
                    raise HTTPException(status_code=500, detail="Android OAuth configuration is missing")
                client_id = self.google_android_client_id
                client_secret = None  # Android apps don't use client secrets
                redirect_uri = self.google_android_redirect_uri
            else:
                # Web credentials (web apps require client secret)
                client_id = self.google_client_id
                client_secret = self.google_client_secret
                redirect_uri = self.google_redirect_uri
            
            # Exchange code for token
            async with httpx.AsyncClient() as client:
                token_data = {
                    'client_id': client_id,
                    'code': code,
                    'grant_type': 'authorization_code',
                    'redirect_uri': redirect_uri
                }
                
                # Only add client_secret for web flows (not mobile)
                if client_secret is not None:
                    token_data['client_secret'] = client_secret
                
                # Add code_verifier for PKCE flows (mobile apps)
                if code_verifier is not None:
                    token_data['code_verifier'] = code_verifier
                
                # Log the request data (without sensitive info)
                logger.info(f"Token exchange request - client_id: {client_id}, redirect_uri: {redirect_uri}, device: {device}, has_code_verifier: {code_verifier is not None}")
                
                token_response = await client.post(self.google_token_url, data=token_data)
                
                # Log the response for debugging
                if not token_response.is_success:
                    response_text = token_response.text
                    logger.error(f"Google token exchange failed. Status: {token_response.status_code}, Response: {response_text}")
                    raise HTTPException(status_code=400, detail=f"Google token exchange failed: {response_text}")
                
                token_response.raise_for_status()
                tokens = token_response.json()
                
                # Get user info from Google
                headers = {'Authorization': f"Bearer {tokens['access_token']}"}
                user_response = await client.get(self.google_userinfo_url, headers=headers)
                user_response.raise_for_status()
                user_data = user_response.json()
                
                # Create or get existing user
                return await self._create_or_get_oauth_user(user_data, 'google')
                
        except httpx.HTTPError as e:
            logger.error(f"HTTP error in Google OAuth: {str(e)}")
            raise HTTPException(status_code=400, detail="Failed to authenticate with Google")
        except Exception as e:
            logger.error(f"Error in Google OAuth callback: {str(e)}")
            raise HTTPException(status_code=500, detail="OAuth authentication failed")

    def _generate_apple_client_secret(self) -> str:
        """Generate Apple client secret JWT"""
        try:
            headers = {
                'alg': 'ES256',
                'kid': self.apple_key_id
            }
            
            now = datetime.now(timezone.utc)
            payload = {
                'iss': self.apple_team_id,
                'iat': int(now.timestamp()),
                'exp': int((now + timedelta(hours=1)).timestamp()),
                'aud': 'https://appleid.apple.com',
                'sub': self.apple_client_id
            }
            
            return jwt.encode(payload, self.apple_private_key, algorithm='ES256', headers=headers)
            
        except Exception as e:
            logger.error(f"Error generating Apple client secret: {str(e)}")
            raise HTTPException(status_code=500, detail="Failed to generate Apple client secret")

    async def handle_apple_callback(self, code: str, id_token: str, state: Optional[str] = None, user_data: Optional[str] = None) -> Dict[str, Any]:
        """Handle Apple OAuth callback and create/login user"""
        try:
            # Decode the ID token to get user information
            # Note: In production, you should verify the JWT signature
            decoded_token = jwt.decode(id_token, options={"verify_signature": False})
            
            # Parse user data if provided (only provided on first sign-in)
            user_info = {}
            if user_data:
                try:
                    user_info = json.loads(user_data)
                except json.JSONDecodeError:
                    logger.warning("Failed to parse Apple user data")
            
            # Prepare user data for our system
            apple_user_data = {
                'id': decoded_token.get('sub'),
                'email': decoded_token.get('email', ''),
                'email_verified': decoded_token.get('email_verified', True),
                'given_name': user_info.get('name', {}).get('firstName', '') if user_info else APPLE_DEFAULT_FIRST_NAME,
                'family_name': user_info.get('name', {}).get('lastName', '') if user_info else APPLE_DEFAULT_LAST_NAME,
                'name': ''
            }

            if not apple_user_data['given_name']:
                apple_user_data['given_name'] = APPLE_DEFAULT_FIRST_NAME
            if not apple_user_data['family_name']:
                apple_user_data['family_name'] = APPLE_DEFAULT_LAST_NAME
            
            # If we have names, construct full name
            if apple_user_data['given_name'] or apple_user_data['family_name']:
                apple_user_data['name'] = f"{apple_user_data['given_name']} {apple_user_data['family_name']}".strip()
            
            # Create or get existing user
            return await self._create_or_get_oauth_user(apple_user_data, 'apple')
            
        except jwt.DecodeError as e:
            logger.error(f"Error decoding Apple ID token: {str(e)}")
            raise HTTPException(status_code=400, detail="Invalid Apple ID token")
        except Exception as e:
            logger.error(f"Error in Apple OAuth callback: {str(e)}")
            raise HTTPException(status_code=500, detail="OAuth authentication failed")

    async def _create_or_get_oauth_user(self, user_data: Dict[str, Any], provider: str) -> Dict[str, Any]:
        """Create or get existing OAuth user"""
        try:
            with self.db.get_session() as session:
                # Check if user exists by OAuth ID
                oauth_id = str(user_data.get('id', ''))
                existing_user = session.query(User).filter(
                    User.oauth_provider == provider,
                    User.oauth_id == oauth_id
                ).first()
                
                if existing_user:
                    # User exists, generate token and return
                    token = jwt.encode(
                        {
                            "email": existing_user.email,
                            "user_id": str(existing_user.id)
                        },
                        os.getenv("SECRET_KEY"),
                        algorithm="HS256"
                    )
                    
                    return {
                        "token": token,
                        "user": {
                            "id": str(existing_user.id),
                            "email": existing_user.email,
                            "first_name": existing_user.first_name,
                            "last_name": existing_user.last_name,
                            "user_name": existing_user.user_name,
                            "professional_type": existing_user.professional_type,
                            "oauth_provider": existing_user.oauth_provider,
                            "date_of_birth": existing_user.date_of_birth.isoformat() if existing_user.date_of_birth else None,
                            "country": existing_user.country,
                        },
                        "user_type": existing_user.professional_type or "User",
                        "is_new_user": False,
                        "requires_profile_completion": self._requires_profile_completion(existing_user),
                    }
                
                # Check if user exists by email (to handle email conflicts)
                email = (user_data.get('email') or '').strip().lower() or None
                if email:
                    existing_email_user = session.query(User).filter(User.email == email).first()
                    if existing_email_user and not existing_email_user.oauth_provider:
                        # User exists with same email but different auth method
                        raise HTTPException(
                            status_code=409, 
                            detail=f"An account with this email already exists. Please sign in with your password or link your {provider.title()} account."
                        )
                
                # Create new user
                user_name = self._generate_unique_username(
                    session, 
                    user_data.get('given_name', '') + user_data.get('family_name', ''), 
                    email or f"{provider}{oauth_id}"
                )
                
                new_user = User(
                    first_name=user_data.get('given_name', user_data.get('name', '').split(' ')[0] if user_data.get('name') else ''),
                    last_name=user_data.get('family_name', ' '.join(user_data.get('name', '').split(' ')[1:]) if user_data.get('name') else ''),
                    user_name=user_name,
                    email=email,
                    hashed_password='',  # No password for OAuth users
                    client='oauth_' + provider,
                    address='',  # Will be filled later if needed
                    town_city='',
                    country='',
                    postcode='',
                    oauth_provider=provider,
                    oauth_id=oauth_id,
                    is_email_verified=bool(email),
                    created_at=datetime.now(timezone.utc),
                    updated_at=datetime.now(timezone.utc)
                )
                
                session.add(new_user)
                session.commit()
                session.refresh(new_user)
                
                # Generate token
                token = jwt.encode(
                    {
                        "email": new_user.email,
                        "user_id": str(new_user.id)
                    },
                    os.getenv("SECRET_KEY"),
                    algorithm="HS256"
                )
                
                return {
                    "token": token,
                    "user": {
                        "id": str(new_user.id),
                        "email": new_user.email,
                        "first_name": new_user.first_name,
                        "last_name": new_user.last_name,
                        "user_name": new_user.user_name,
                        "professional_type": new_user.professional_type,
                        "oauth_provider": new_user.oauth_provider,
                        "date_of_birth": None,
                        "country": new_user.country,
                    },
                    "user_type": new_user.professional_type or "User",
                    "is_new_user": True,
                    "requires_profile_completion": True,
                }
                
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error creating/getting OAuth user: {str(e)}")
            raise HTTPException(status_code=500, detail="Failed to process OAuth authentication")

    @staticmethod
    def _requires_profile_completion(user: User) -> bool:
        return any(
            not value
            for value in (
                user.email,
                user.first_name,
                user.last_name,
                user.date_of_birth,
            )
        )

    def _generate_unique_username(self, session, preferred_name: str, email: str) -> str:
        """Generate a unique username"""
        # Clean the preferred name
        base_username = re.sub(r'[^a-zA-Z0-9]', '', preferred_name.lower())
        if not base_username:
            base_username = email.split('@')[0] if email else 'user'
        
        base_username = base_username[:20]  # Limit length
        
        # Check if username exists
        username = base_username
        counter = 1
        while session.query(User).filter(User.user_name == username).first():
            username = f"{base_username}{counter}"
            counter += 1
            if counter > 9999:  # Prevent infinite loop
                username = f"{base_username}_{str(uuid.uuid4())[:8]}"
                break
        
        return username

    def link_oauth_to_user(self, user_id: str, provider: str, oauth_id: str, oauth_email: str) -> bool:
        """Link OAuth account to existing user"""
        try:
            with self.db.get_session() as session:
                user = session.query(User).filter(User.id == user_id).first()
                if not user:
                    raise HTTPException(status_code=404, detail="User not found")
                
                # Check if OAuth account is already linked to another user
                existing_oauth = session.query(User).filter(
                    User.oauth_provider == provider,
                    User.oauth_id == oauth_id,
                    User.id != user_id
                ).first()
                
                if existing_oauth:
                    raise HTTPException(
                        status_code=409, 
                        detail=f"This {provider} account is already linked to another user"
                    )
                
                # Link the OAuth account
                user.oauth_provider = provider
                user.oauth_id = oauth_id
                user.is_email_verified = True  # OAuth emails are verified
                session.commit()
                
                return True
                
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error linking OAuth account: {str(e)}")
            raise HTTPException(status_code=500, detail="Failed to link OAuth account")

    def unlink_oauth_from_user(self, user_id: str, provider: str) -> bool:
        """Unlink OAuth account from user"""
        try:
            with self.db.get_session() as session:
                user = session.query(User).filter(User.id == user_id).first()
                if not user:
                    raise HTTPException(status_code=404, detail="User not found")
                
                # Check if this is an OAuth-only account
                if user.oauth_provider == provider and not user.hashed_password:
                    raise HTTPException(
                        status_code=400, 
                        detail="Cannot unlink primary authentication method. Set a password first."
                    )
                
                # Unlink the OAuth account
                user.oauth_provider = None
                user.oauth_id = None
                session.commit()
                
                return True
                
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error unlinking OAuth account: {str(e)}")
            raise HTTPException(status_code=500, detail="Failed to unlink OAuth account")
