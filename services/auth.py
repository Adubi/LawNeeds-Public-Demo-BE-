from fastapi import HTTPException, Request, UploadFile
from datetime import datetime, timezone, timedelta
import hashlib
import os
import asyncio
import jwt
from dotenv import load_dotenv
import logging
from .models import (
    User, PasswordResetToken, EmailVerificationToken, WorkExperience,
    LEGAL_SERVICES, LEGAL_SKILLS, ReferralRequest, AnnualConsultationCredit,
    DeviceToken,
)
from .profile_taxonomy import (
    validate_practice_ids, validate_service_ids, validate_skill_ids,
    practice_ids_to_labels, service_ids_to_labels, skill_ids_to_labels,
    labels_to_practice_items, labels_to_service_items, labels_to_skill_items,
    _dedupe,
)
from sqlalchemy.exc import SQLAlchemyError, IntegrityError
from .db import DB  # Import the DB class
from .notification_service import NotificationService
from .firebase_notification_service import FirebaseNotificationService
from .password_hashing import hash_password, verify_password
import shutil
import uuid
import boto3
from botocore.exceptions import ClientError
from typing import Optional, List, Dict, Union
import random
import re

# Load environment variables from the .env file
load_dotenv()

# Access the environment variables

SECRET_KEY = os.getenv("SECRET_KEY")
ALGORITHM = "HS256"
# OTP expiration time (10 minutes for security)
PASSWORD_RESET_EXPIRE_MINUTES = 10
EMAIL_VERIFICATION_EXPIRE_MINUTES = 15

logger = logging.getLogger(__name__)

# Required fields for the profile-completeness check (profile_incomplete
# notification). Plain client/end-users are excluded entirely - their
# professional_type will not match any key below, so _is_profile_incomplete
# short-circuits to False for them.
COMMON_REQUIRED_FIELDS = ['first_name', 'last_name', 'phone_number']

PROFILE_REQUIRED_FIELDS = {
    "Lawyer": ['legal_expertise', 'services', 'hourly_rate'],
    "Law Firm": ['firm_name', 'practice_areas', 'firm_address', 'firm_hourly_rate'],
    "HR/Legal (SME)": ['company_name', 'sector', 'services'],
    "A2J Partners": ['organisation_name', 'organisation_type', 'areas_of_legal_focus'],
}

# sra_bsb_iaa OR sra_bsb (either counts) is required verification for these types
SRA_REQUIRED_TYPES = ("Lawyer", "Law Firm", "A2J Partners")

class Auth:
    
    def __init__(self, *, app_name, db: DB):
        self.app_name = app_name
        self.db = db
    
    # Generate random 6-digit OTP
    def generate_otp(self) -> str:
        """Generate a random 6-digit OTP code"""
        return f"{random.randint(100000, 999999):06d}"
    
    # Verify JWT token
    def verify_token(self, token):
        try:
            payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
            
            # If token doesn't have user_id but has email, try to fetch user_id
            if "user_id" not in payload and "email" in payload:
                with self.db.get_session() as session:
                    user = session.query(User).filter(
                        User.email == payload["email"]
                    ).first()
                    if user:
                        payload["user_id"] = str(user.id)
                    else:
                        logger.error(f"No user found for email: {payload['email']}")
                        raise HTTPException(status_code=401, detail="Invalid token")
            
            return payload
        except jwt.PyJWTError:
            logger.error("Invalid token")
            raise HTTPException(status_code=401, detail="Invalid token")
        except Exception as e:
            logger.error(f"Error verifying token: {str(e)}")
            raise HTTPException(status_code=401, detail="Invalid token")
        
    # Verify request token
    def verify_request_token(self, request):
        """
        Verify the token from the request's Authorization header.
        Returns the decoded token payload if valid.
        """
        try:
            # Extract token from Authorization header
            auth_header = request.headers.get("Authorization")
            if not auth_header or not auth_header.startswith("Bearer "):
                raise HTTPException(
                    status_code=401,
                    detail="Missing or invalid Authorization header"
                )
            
            token = auth_header.split(" ")[1]
            return self.verify_token(token)
            
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Invalid token: {str(e)}")
            # Make sure to raise an HTTPException with 401 status code
            raise HTTPException(
                status_code=401,
                detail="Invalid token"
            )
        
    
    
    
    
    
    
    
    
    # Authenticate user
    def auth_user(self, email, password):
        try:
            with self.db.get_session() as session:
                user = session.query(User).filter(
                    User.email == email
                ).first()
                
                if not user:
                    raise HTTPException(
                        status_code=401,
                        detail="Incorrect email or password"
                    )

                password_valid, needs_rehash = verify_password(password, user.hashed_password)
                if not password_valid:
                    raise HTTPException(
                        status_code=401,
                        detail="Incorrect email or password"
                    )

                if needs_rehash:
                    user.hashed_password = hash_password(password)
                    session.commit()
                
                # Include both email and user_id in token
                token = jwt.encode(
                    {
                        "email": user.email,
                        "user_id": str(user.id)  # UUID needs to be converted to string
                    },
                    SECRET_KEY,
                    algorithm=ALGORITHM
                )
                
                # Return both token and user details
                return token, {
                    "id": str(user.id),  # Convert UUID to string
                    "email": user.email,
                    "first_name": user.first_name,
                    "last_name": user.last_name,
                    "user_name": user.user_name,
                    "professional_type": user.professional_type  # Add professional_type to user details
                }
                
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Database error during user authentication: {str(e)}")
            raise HTTPException(status_code=500, detail="Internal server error")
    
    # Create user - simplified for basic user registration only
    def generate_unique_username(self, first_name: str, last_name: str, email: str) -> str:
        """Generate a stable, human-readable username for streamlined signup."""
        preferred_name = f"{first_name}{last_name}".strip().lower()
        base_username = re.sub(r'[^a-z0-9]', '', preferred_name)
        if not base_username:
            base_username = re.sub(r'[^a-z0-9]', '', email.split('@')[0].lower())
        base_username = (base_username or "user")[:60]

        with self.db.get_session() as session:
            candidate = base_username
            suffix = 1
            while session.query(User).filter(User.user_name == candidate).first():
                suffix += 1
                candidate = f"{base_username}{suffix}"
            return candidate

    def create_user(self, 
                    user_name: str, 
                    email: str, 
                    password: str, 
                    client: str, 
                    first_name: Optional[str] = None, 
                    last_name: Optional[str] = None, 
                    date_of_birth: Optional[datetime] = None, 
                    address: Optional[str] = None, 
                    town_city: Optional[str] = None, 
                    country: Optional[str] = None, 
                    postcode: Optional[str] = None):
        try:
            hashed_password = hash_password(password)
            
            # Create a new user with basic information only
            new_user = User(
                first_name=first_name,
                last_name=last_name,
                user_name=user_name,
                email=email,
                hashed_password=hashed_password,
                client=client,
                date_of_birth=date_of_birth,
                address=address,
                town_city=town_city,
                country=country,
                postcode=postcode,
                professional_type=None  # Basic users have no professional type
            )

            with self.db.get_session() as session:
                # Check if user exists
                existing_user = session.query(User).filter(
                    (User.email == email) | (User.user_name == user_name)
                ).first()
                
                if existing_user:
                    raise HTTPException(
                        status_code=400,
                        detail="User with this email or username already exists"
                    )
                
                # Add and commit the new user
                session.add(new_user)
                session.commit()
                
                # Refresh to get the generated ID
                session.refresh(new_user)
                
                # Create token with both email and user_id
                token = jwt.encode(
                    {
                        "email": new_user.email,
                        "user_id": str(new_user.id)
                    },
                    SECRET_KEY,
                    algorithm=ALGORITHM
                )
                
                return token
                
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error creating user: {str(e)}")
            raise HTTPException(status_code=500, detail="Internal server error")

    # Create professional user - handles all professional user types
    def create_professional_user(self, 
                    user_name: str, 
                    email: str, 
                    password: str, 
                    client: str, 
                    professional_type: str,
                    first_name: Optional[str] = None, 
                    last_name: Optional[str] = None, 
                    date_of_birth: Optional[datetime] = None, 
                    address: Optional[str] = None, 
                    town_city: Optional[str] = None, 
                    country: Optional[str] = None, 
                    postcode: Optional[str] = None,
                    # Lawyer specific fields
                    sra_bsb_iaa: Optional[str] = None,
                    professional_employer: Optional[str] = None,
                    legal_expertise: Optional[List[str]] = None,
                    proof_of_id: Optional[str] = None,
                    proof_of_id_type: Optional[str] = None,
                    # Law Firm specific fields
                    firm_name: Optional[str] = None,
                    date_of_incorporation: Optional[datetime] = None,
                    sra_bsb: Optional[str] = None,
                    practice_areas: Optional[List[str]] = None,
                    contact_person: Optional[str] = None,
                    company_registration_number: Optional[str] = None,
                    # HR/Legal (SME) specific fields
                    hr_legal_type: Optional[str] = None,
                    sector: Optional[str] = None,
                    company_name: Optional[str] = None,
                    # A2J Partners specific fields
                    organisation_name: Optional[str] = None,
                    date_of_establishment: Optional[datetime] = None,
                    organisation_type: Optional[str] = None,
                    areas_of_legal_focus: Optional[List[str]] = None,
                    registration_charity_number: Optional[str] = None):
        try:
            hashed_password = hash_password(password)
            
            # Create a new professional user with basic information
            new_user = User(
                first_name=first_name,
                last_name=last_name,
                user_name=user_name,
                email=email,
                hashed_password=hashed_password,
                client=client,
                date_of_birth=date_of_birth,
                address=address,
                town_city=town_city,
                country=country,
                postcode=postcode,
                professional_type=professional_type
            )
            
            # Add professional-specific fields that are common to all types
            if sra_bsb_iaa is not None:
                new_user.sra_bsb_iaa = sra_bsb_iaa
            if sra_bsb is not None:
                new_user.sra_bsb = sra_bsb
            
            # Add professional-specific fields based on the professional type
            if professional_type == "Lawyer":
                new_user.professional_employer = professional_employer
                new_user.legal_expertise = legal_expertise if legal_expertise else []
                new_user.proof_of_id = proof_of_id
                new_user.proof_of_id_type = proof_of_id_type
            
            elif professional_type == "Law Firm":
                new_user.firm_name = firm_name
                new_user.date_of_incorporation = date_of_incorporation
                new_user.practice_areas = practice_areas if practice_areas else []
                new_user.contact_person = contact_person
                new_user.company_registration_number = company_registration_number
            
            elif professional_type == "HR/Legal (SME)":
                new_user.hr_legal_type = hr_legal_type
                new_user.sector = sector
                new_user.company_name = company_name
                new_user.company_registration_number = company_registration_number
            
            elif professional_type == "A2J Partners":
                new_user.organisation_name = organisation_name
                new_user.date_of_establishment = date_of_establishment
                new_user.organisation_type = organisation_type
                new_user.areas_of_legal_focus = areas_of_legal_focus if areas_of_legal_focus else []
                new_user.contact_person = contact_person
                new_user.registration_charity_number = registration_charity_number

            with self.db.get_session() as session:
                # Check if user exists
                existing_user = session.query(User).filter(
                    (User.email == email) | (User.user_name == user_name)
                ).first()
                
                if existing_user:
                    raise HTTPException(
                        status_code=400,
                        detail="User with this email or username already exists"
                    )
                
                # Add and commit the new user
                session.add(new_user)
                session.commit()
                
                # Refresh to get the generated ID
                session.refresh(new_user)
                
                # Create token with both email and user_id
                token = jwt.encode(
                    {
                        "email": new_user.email,
                        "user_id": str(new_user.id)
                    },
                    SECRET_KEY,
                    algorithm=ALGORITHM
                )
                
                return token
                
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error creating professional user: {str(e)}")
            raise HTTPException(status_code=500, detail="Internal server error")

    def _is_profile_incomplete(self, user) -> bool:
        """
        Returns True if any required field (common + type-specific) is
        missing for the user's professional_type. Non-professional users
        (professional_type not in PROFILE_REQUIRED_FIELDS) are never
        considered incomplete - per product decision, they are excluded
        from this notification entirely.
        """
        for field in COMMON_REQUIRED_FIELDS:
            if not getattr(user, field, None):
                return True

        type_fields = PROFILE_REQUIRED_FIELDS.get(user.professional_type)
        if type_fields is None:
            return False

        for field in type_fields:
            if not getattr(user, field, None):
                return True

        if user.professional_type in SRA_REQUIRED_TYPES:
            if not getattr(user, 'sra_bsb_iaa', None) and not getattr(user, 'sra_bsb', None):
                return True

        return False

    def update_user_profile(self, user_id, profile_data):
        """
        Update user profile with the provided data including professional-specific fields.
        OAuth accounts may add/change their email; all users may complete their username
        and deferred personal/address fields. Password changes remain excluded.
        """
        try:
            with self.db.get_session() as session:
                user = session.query(User).filter(User.id == user_id).first()
                
                if not user:
                    raise HTTPException(
                        status_code=404,
                        detail="User not found"
                    )
                
                # Update basic fields if they are provided
                if profile_data.get('email') is not None:
                    if not user.oauth_provider:
                        raise HTTPException(
                            status_code=400,
                            detail="Email can only be added here for OAuth accounts",
                        )
                    normalized_email = str(profile_data['email']).strip().lower()
                    if not normalized_email:
                        raise HTTPException(status_code=400, detail="Email is required")
                    email_owner = session.query(User).filter(
                        User.email == normalized_email,
                        User.id != user.id,
                    ).first()
                    if email_owner:
                        raise HTTPException(status_code=409, detail="An account with this email already exists")
                    email_changed = normalized_email != user.email
                    user.email = normalized_email
                    if email_changed:
                        user.is_email_verified = False
                if profile_data.get('user_name') is not None:
                    normalized_user_name = profile_data['user_name'].strip()
                    if len(normalized_user_name) < 3:
                        raise HTTPException(status_code=400, detail="Username must contain at least 3 characters")
                    username_owner = session.query(User).filter(
                        User.user_name == normalized_user_name,
                        User.id != user.id,
                    ).first()
                    if username_owner:
                        raise HTTPException(status_code=409, detail="This username is already in use")
                    user.user_name = normalized_user_name
                if profile_data.get('first_name') is not None:
                    user.first_name = profile_data['first_name']
                if profile_data.get('last_name') is not None:
                    user.last_name = profile_data['last_name']
                if profile_data.get('date_of_birth') is not None:
                    user.date_of_birth = profile_data['date_of_birth']
                if profile_data.get('address') is not None:
                    user.address = profile_data['address']
                if profile_data.get('town_city') is not None:
                    user.town_city = profile_data['town_city']
                if profile_data.get('country') is not None:
                    user.country = profile_data['country']
                if profile_data.get('postcode') is not None:
                    user.postcode = profile_data['postcode']
                
                # Update new fields for all users
                if profile_data.get('about') is not None:
                    user.about = profile_data['about']
                if profile_data.get('tagline') is not None:
                    user.tagline = profile_data['tagline']
                if profile_data.get('phone_number') is not None:
                    user.phone_number = profile_data['phone_number']
                if profile_data.get('immigration_status') is not None:
                    user.immigration_status = profile_data['immigration_status']
                if profile_data.get('webinar') is not None:
                    user.webinar = profile_data['webinar']
                
                # Handle work experiences update
                if profile_data.get('work_experiences') is not None:
                    self._update_work_experiences(session, user_id, profile_data['work_experiences'])
                
                # Update professional-specific fields based on user type
                if user.professional_type == "Lawyer":
                    if profile_data.get('sra_bsb_iaa') is not None:
                        user.sra_bsb_iaa = profile_data['sra_bsb_iaa']
                    if profile_data.get('sra_bsb') is not None:
                        user.sra_bsb = profile_data['sra_bsb']
                    if profile_data.get('professional_employer') is not None:
                        user.professional_employer = profile_data['professional_employer']
                    if profile_data.get('legal_expertise_ids') is not None:
                        ids = _dedupe(profile_data['legal_expertise_ids'])
                        validate_practice_ids(ids)
                        user.legal_expertise = practice_ids_to_labels(ids)
                    elif profile_data.get('legal_expertise') is not None:
                        user.legal_expertise = profile_data['legal_expertise']
                    if profile_data.get('service_ids') is not None:
                        ids = _dedupe(profile_data['service_ids'])
                        validate_service_ids(ids)
                        user.services = service_ids_to_labels(ids)
                    elif profile_data.get('services') is not None:
                        user.services = profile_data['services']
                    if profile_data.get('skill_ids') is not None:
                        ids = _dedupe(profile_data['skill_ids'])
                        validate_skill_ids(ids)
                        user.skills = skill_ids_to_labels(ids)
                    elif profile_data.get('skills') is not None:
                        user.skills = profile_data['skills']
                    if profile_data.get('hourly_rate') is not None:
                        user.hourly_rate = profile_data['hourly_rate']
                
                elif user.professional_type == "Law Firm":
                    if profile_data.get('firm_name') is not None:
                        user.firm_name = profile_data['firm_name']
                    if profile_data.get('sra_bsb') is not None:
                        user.sra_bsb = profile_data['sra_bsb']
                    if profile_data.get('sra_bsb_iaa') is not None:
                        user.sra_bsb_iaa = profile_data['sra_bsb_iaa']
                    if profile_data.get('practice_area_ids') is not None:
                        ids = _dedupe(profile_data['practice_area_ids'])
                        validate_practice_ids(ids)
                        user.practice_areas = practice_ids_to_labels(ids)
                    elif profile_data.get('practice_areas') is not None:
                        user.practice_areas = profile_data['practice_areas']
                    if profile_data.get('contact_person') is not None:
                        user.contact_person = profile_data['contact_person']
                    if profile_data.get('company_registration_number') is not None:
                        user.company_registration_number = profile_data['company_registration_number']
                    if profile_data.get('firm_address') is not None:
                        user.firm_address = profile_data['firm_address']
                    if profile_data.get('firm_website') is not None:
                        user.firm_website = profile_data['firm_website']
                    if profile_data.get('firm_phone_number') is not None:
                        user.firm_phone_number = profile_data['firm_phone_number']
                    if profile_data.get('service_ids') is not None:
                        ids = _dedupe(profile_data['service_ids'])
                        validate_service_ids(ids)
                        user.services = service_ids_to_labels(ids)
                    elif profile_data.get('services') is not None:
                        user.services = profile_data['services']
                    if profile_data.get('firm_hourly_rate') is not None:
                        user.firm_hourly_rate = profile_data['firm_hourly_rate']
                
                elif user.professional_type == "HR/Legal (SME)":
                    if profile_data.get('hr_legal_type') is not None:
                        user.hr_legal_type = profile_data['hr_legal_type']
                    if profile_data.get('sector') is not None:
                        user.sector = profile_data['sector']
                    if profile_data.get('company_name') is not None:
                        user.company_name = profile_data['company_name']
                    if profile_data.get('company_registration_number') is not None:
                        user.company_registration_number = profile_data['company_registration_number']
                    # Add sra_bsb fields for HR/Legal SMEs
                    if profile_data.get('sra_bsb_iaa') is not None:
                        user.sra_bsb_iaa = profile_data['sra_bsb_iaa']
                    if profile_data.get('sra_bsb') is not None:
                        user.sra_bsb = profile_data['sra_bsb']
                    if profile_data.get('service_ids') is not None:
                        ids = _dedupe(profile_data['service_ids'])
                        validate_service_ids(ids)
                        user.services = service_ids_to_labels(ids)
                    elif profile_data.get('services') is not None:
                        user.services = profile_data['services']
                    if profile_data.get('skill_ids') is not None:
                        ids = _dedupe(profile_data['skill_ids'])
                        validate_skill_ids(ids)
                        user.skills = skill_ids_to_labels(ids)
                    elif profile_data.get('skills') is not None:
                        user.skills = profile_data['skills']
                    
                elif user.professional_type == "A2J Partners":
                    if profile_data.get('organisation_name') is not None:
                        user.organisation_name = profile_data['organisation_name']
                    if profile_data.get('date_of_establishment') is not None:
                        user.date_of_establishment = profile_data['date_of_establishment']
                    if profile_data.get('organisation_type') is not None:
                        user.organisation_type = profile_data['organisation_type']
                    if profile_data.get('areas_of_legal_focus_ids') is not None:
                        ids = _dedupe(profile_data['areas_of_legal_focus_ids'])
                        validate_practice_ids(ids)
                        user.areas_of_legal_focus = practice_ids_to_labels(ids)
                    elif profile_data.get('areas_of_legal_focus') is not None:
                        user.areas_of_legal_focus = profile_data['areas_of_legal_focus']
                    if profile_data.get('contact_person') is not None:
                        user.contact_person = profile_data['contact_person']
                    if profile_data.get('registration_charity_number') is not None:
                        user.registration_charity_number = profile_data['registration_charity_number']
                    if profile_data.get('a2j_website') is not None:
                        user.a2j_website = profile_data['a2j_website']
                    if profile_data.get('sra_bsb_iaa') is not None:
                        user.sra_bsb_iaa = profile_data['sra_bsb_iaa']
                    if profile_data.get('sra_bsb') is not None:
                        user.sra_bsb = profile_data['sra_bsb']
                    if profile_data.get('service_ids') is not None:
                        ids = _dedupe(profile_data['service_ids'])
                        validate_service_ids(ids)
                        user.services = service_ids_to_labels(ids)
                    elif profile_data.get('services') is not None:
                        user.services = profile_data['services']
                    if profile_data.get('skill_ids') is not None:
                        ids = _dedupe(profile_data['skill_ids'])
                        validate_skill_ids(ids)
                        user.skills = skill_ids_to_labels(ids)
                    elif profile_data.get('skills') is not None:
                        user.skills = profile_data['skills']
                
                # Profile-completeness check (profile_incomplete notification).
                # Fires once only per user - profile_incomplete_sent_at is never
                # cleared, so a user who was flagged once will not be re-notified
                # even if they remain incomplete on later saves.
                if not user.profile_incomplete_sent_at and self._is_profile_incomplete(user):
                    NotificationService(session).create(
                        recipient_id=user.id,
                        title="Complete Your Profile",
                        body="A few details are still missing from your profile. Finish it to get the most out of LawNeeds.",
                        notification_type="profile_incomplete",
                        data={"user_id": str(user.id), "screen": "ProfileEdit"},
                    )
                    user.profile_incomplete_sent_at = datetime.now(timezone.utc)

                    # Best-effort push notification, fired as a background task on
                    # the current event loop. update_user_profile() is synchronous
                    # but is called from an async FastAPI endpoint (see api/app.py
                    # update_profile()), so a loop is running on this thread.
                    try:
                        device_tokens = session.query(DeviceToken).filter(
                            DeviceToken.user_id == user.id,
                            DeviceToken.is_active == True
                        ).all()
                        tokens = [dt.device_token for dt in device_tokens]
                        if tokens:
                            firebase_service = FirebaseNotificationService()
                            loop = asyncio.get_running_loop()
                            loop.create_task(
                                firebase_service.send_profile_incomplete_notification(
                                    device_tokens=tokens,
                                    user_id=str(user.id),
                                )
                            )
                    except RuntimeError:
                        logger.warning("No running event loop; skipping profile_incomplete push notification")
                    except Exception as push_err:
                        logger.error("Error queuing profile_incomplete push notification: " + str(push_err))

                session.commit()
                
                # Return updated profile using the get_user_profile method
                return self.get_user_profile(user_id)
                
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error updating user profile: {str(e)}")
            raise HTTPException(status_code=500, detail="Internal server error")

    def get_user_profile(self, user_id):
        """
        Get user profile data including professional-specific fields.
        """
        try:
            with self.db.get_session() as session:
                user = session.query(User).filter(User.id == user_id).first()
                
                if not user:
                    raise HTTPException(
                        status_code=404,
                        detail="User not found"
                    )
                
                # Base user details
                profile_data = {
                    "id": str(user.id),
                    "email": user.email,
                    "first_name": user.first_name,
                    "last_name": user.last_name,
                    "user_name": user.user_name,
                    "address": user.address,
                    "town_city": user.town_city,
                    "country": user.country,
                    "postcode": user.postcode,
                    "date_of_birth": user.date_of_birth.isoformat() if user.date_of_birth else None,
                    "profile_picture_path": user.profile_picture_path,
                    "professional_type": user.professional_type,
                    "created_at": user.created_at.isoformat() if user.created_at else None,
                    "updated_at": user.updated_at.isoformat() if user.updated_at else None,
                    # New fields for all users
                    "about": user.about,
                    "tagline": user.tagline,
                    "phone_number": user.phone_number,
                    "immigration_status": user.immigration_status,
                    "webinar": user.webinar,
                    "oauth_provider": user.oauth_provider,
                }
                
                # Add professional-specific fields based on user type
                if user.professional_type == "Lawyer":
                    le_ids, le_items = labels_to_practice_items(user.legal_expertise)
                    sv_ids, sv_items = labels_to_service_items(user.services)
                    sk_ids, sk_items = labels_to_skill_items(user.skills)
                    profile_data.update({
                        "sra_bsb_iaa": user.sra_bsb_iaa,
                        "sra_bsb": user.sra_bsb,
                        "professional_employer": user.professional_employer,
                        "legal_expertise_ids": le_ids,
                        "legal_expertise": le_items,
                        "proof_of_id": user.proof_of_id,
                        "proof_of_id_type": user.proof_of_id_type,
                        "service_ids": sv_ids,
                        "services": sv_items,
                        "skill_ids": sk_ids,
                        "skills": sk_items,
                        "hourly_rate": float(user.hourly_rate) if user.hourly_rate else None
                    })
                elif user.professional_type == "Law Firm":
                    pa_ids, pa_items = labels_to_practice_items(user.practice_areas)
                    sv_ids, sv_items = labels_to_service_items(user.services)
                    profile_data.update({
                        "firm_name": user.firm_name,
                        "date_of_incorporation": user.date_of_incorporation.isoformat() if user.date_of_incorporation else None,
                        "sra_bsb": user.sra_bsb,
                        "sra_bsb_iaa": user.sra_bsb_iaa,
                        "practice_area_ids": pa_ids,
                        "practice_areas": pa_items,
                        "contact_person": user.contact_person,
                        "company_registration_number": user.company_registration_number,
                        "firm_address": user.firm_address,
                        "firm_website": user.firm_website,
                        "firm_phone_number": user.firm_phone_number,
                        "service_ids": sv_ids,
                        "services": sv_items,
                        "firm_hourly_rate": float(user.firm_hourly_rate) if user.firm_hourly_rate else None
                    })
                elif user.professional_type == "HR/Legal (SME)":
                    sv_ids, sv_items = labels_to_service_items(user.services)
                    sk_ids, sk_items = labels_to_skill_items(user.skills)
                    profile_data.update({
                        "hr_legal_type": user.hr_legal_type,
                        "sector": user.sector,
                        "company_name": user.company_name,
                        "company_registration_number": user.company_registration_number,
                        "sra_bsb_iaa": user.sra_bsb_iaa,
                        "sra_bsb": user.sra_bsb,
                        "service_ids": sv_ids,
                        "services": sv_items,
                        "skill_ids": sk_ids,
                        "skills": sk_items,
                    })
                elif user.professional_type == "A2J Partners":
                    af_ids, af_items = labels_to_practice_items(user.areas_of_legal_focus)
                    sv_ids, sv_items = labels_to_service_items(user.services)
                    sk_ids, sk_items = labels_to_skill_items(user.skills)
                    profile_data.update({
                        "organisation_name": user.organisation_name,
                        "date_of_establishment": user.date_of_establishment.isoformat() if user.date_of_establishment else None,
                        "organisation_type": user.organisation_type,
                        "areas_of_legal_focus_ids": af_ids,
                        "areas_of_legal_focus": af_items,
                        "contact_person": user.contact_person,
                        "registration_charity_number": user.registration_charity_number,
                        "a2j_website": user.a2j_website,
                        "sra_bsb_iaa": user.sra_bsb_iaa,
                        "sra_bsb": user.sra_bsb,
                        "service_ids": sv_ids,
                        "services": sv_items,
                        "skill_ids": sk_ids,
                        "skills": sk_items,
                    })
                
                # Get work experiences for all users
                work_experiences = session.query(WorkExperience).filter(
                    WorkExperience.user_id == user_id
                ).order_by(WorkExperience.created_at.desc()).all()
                
                profile_data["work_experiences"] = [
                    {
                        "id": str(we.id),
                        "company_name": we.company_name,
                        "achievement": we.achievement,
                        "start_date": we.start_date.isoformat() if we.start_date else None,
                        "end_date": we.end_date.isoformat() if we.end_date else None,
                        "created_at": we.created_at.isoformat() if we.created_at else None,
                        "updated_at": we.updated_at.isoformat() if we.updated_at else None
                    }
                    for we in work_experiences
                ]
                
                return profile_data
                
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error getting user profile: {str(e)}")
            raise HTTPException(status_code=500, detail="Internal server error")

    def upload_profile_picture(self, user_id, file: UploadFile):
        """
        Upload a profile picture for a user to Amazon S3.
        Updates the user's profile_picture_path with the S3 URL.
        """
        try:
            # Create S3 client
            s3_client = boto3.client(
                's3',
                aws_access_key_id=os.getenv('AWS_ACCESS_KEY_ID'),
                aws_secret_access_key=os.getenv('AWS_SECRET_ACCESS_KEY'),
                region_name=os.getenv('AWS_REGION', 'us-east-1')
            )
            
            # S3 bucket name
            bucket_name = os.getenv('S3_BUCKET_NAME', 'lawneeds-profile-pictures')
            
            with self.db.get_session() as session:
                user = session.query(User).filter(User.id == user_id).first()
                
                if not user:
                    raise HTTPException(
                        status_code=404,
                        detail="User not found"
                    )
                
                # Generate a unique key for the S3 object
                file_extension = os.path.splitext(file.filename)[1]
                s3_key = f"profile_pictures/{user_id}/{uuid.uuid4()}{file_extension}"
                
                # Upload file to S3 without ACL parameter
                file_content = file.file.read()
                s3_client.put_object(
                    Bucket=bucket_name,
                    Key=s3_key,
                    Body=file_content,
                    ContentType=file.content_type,
                    CacheControl="public, max-age=31536000, immutable"
                )
                
                # Generate the public S3 URL
                s3_url = f"https://{bucket_name}.s3.amazonaws.com/{s3_key}"
                
                # Delete old profile picture from S3 if it exists
                if user.profile_picture_path and user.profile_picture_path.startswith(f"https://{bucket_name}.s3.amazonaws.com/"):
                    old_key = user.profile_picture_path.replace(f"https://{bucket_name}.s3.amazonaws.com/", "")
                    try:
                        s3_client.delete_object(
                            Bucket=bucket_name,
                            Key=old_key
                        )
                    except ClientError as e:
                        logger.warning(f"Failed to delete old profile picture: {str(e)}")
                
                # Update the user's profile_picture_path with the S3 URL
                user.profile_picture_path = s3_url
                session.commit()
                
                return {
                    "url": s3_url,
                    "key": s3_key
                }
                
        except ClientError as e:
            logger.error(f"S3 error: {str(e)}")
            raise HTTPException(status_code=500, detail="Failed to upload to S3")
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error uploading profile picture: {str(e)}")
            raise HTTPException(status_code=500, detail="Failed to upload profile picture")

    def get_profile_picture_url(self, user_id):
        """
        Get a pre-signed URL for the user's profile picture.
        """
        try:
            with self.db.get_session() as session:
                user = session.query(User).filter(User.id == user_id).first()
                
                if not user or not user.profile_picture_path:
                    return None
                
                # Extract the key from the stored URL
                bucket_name = os.getenv('S3_BUCKET_NAME', 'lawneeds-profile-pictures')
                s3_key = user.profile_picture_path.replace(f"https://{bucket_name}.s3.amazonaws.com/", "")
                
                # Create S3 client
                s3_client = boto3.client(
                    's3',
                    aws_access_key_id=os.getenv('AWS_ACCESS_KEY_ID'),
                    aws_secret_access_key=os.getenv('AWS_SECRET_ACCESS_KEY'),
                    region_name=os.getenv('AWS_REGION', 'us-east-1')
                )
                
                # Generate a pre-signed URL that expires in 1 hour
                presigned_url = s3_client.generate_presigned_url(
                    'get_object',
                    Params={
                        'Bucket': bucket_name,
                        'Key': s3_key
                    },
                    ExpiresIn=3600  # 1 hour
                )
                
                return presigned_url
                
        except Exception as e:
            logger.error(f"Error generating pre-signed URL: {str(e)}")
            return None
    def delete_profile_picture(self, user_id):
        """
        Delete a user's profile picture from S3.
        """
        try:
            # Create S3 client
            s3_client = boto3.client(
                's3',
                aws_access_key_id=os.getenv('AWS_ACCESS_KEY_ID'),
                aws_secret_access_key=os.getenv('AWS_SECRET_ACCESS_KEY'),
                region_name=os.getenv('AWS_REGION', 'us-east-1')
            )
            
            # S3 bucket name
            bucket_name = os.getenv('S3_BUCKET_NAME', 'lawneeds-profile-pictures')
            
            with self.db.get_session() as session:
                user = session.query(User).filter(User.id == user_id).first()
                
                if not user:
                    raise HTTPException(
                        status_code=404,
                        detail="User not found"
                    )
                
                if not user.profile_picture_path:
                    return {"message": "No profile picture to delete"}
                
                # Delete the file from S3 if it's an S3 URL
                if user.profile_picture_path.startswith(f"https://{bucket_name}.s3.amazonaws.com/"):
                    s3_key = user.profile_picture_path.replace(f"https://{bucket_name}.s3.amazonaws.com/", "")
                    s3_client.delete_object(
                        Bucket=bucket_name,
                        Key=s3_key
                    )
                
                # Update the user record
                user.profile_picture_path = None
                session.commit()
                
                return {"message": "Profile picture deleted successfully"}
                
        except ClientError as e:
            logger.error(f"S3 error: {str(e)}")
            raise HTTPException(status_code=500, detail="Failed to delete from S3")
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error deleting profile picture: {str(e)}")
            raise HTTPException(status_code=500, detail="Failed to delete profile picture")

    # Create password reset OTP
    def create_password_reset_token(self, email: str) -> Union[Dict, None]:
        """
        Creates a password reset OTP for the user with the given email.
        Returns the OTP and user information if successful, None if user not found.
        """
        try:
            with self.db.get_session() as session:
                user = session.query(User).filter(User.email == email).first()
                
                if not user:
                    logger.warning(f"Password reset requested for non-existent email: {email}")
                    return None
                
                # Generate 6-digit OTP
                otp_code = self.generate_otp()
                expires = datetime.now(timezone.utc) + timedelta(minutes=PASSWORD_RESET_EXPIRE_MINUTES)
                
                # Create a hash of the OTP for storage (additional security)
                otp_hash = hashlib.sha256(f"{otp_code}{email}".encode()).hexdigest()
                
                # Clean up old unused OTPs for this user
                session.query(PasswordResetToken).filter(
                    PasswordResetToken.user_id == user.id,
                    PasswordResetToken.used == False,
                    PasswordResetToken.expires_at < datetime.now(timezone.utc)
                ).delete()
                
                # Store OTP in database
                reset_token_record = PasswordResetToken(
                    token_hash=otp_hash,
                    otp_code=otp_code,
                    user_id=user.id,
                    expires_at=expires
                )
                session.add(reset_token_record)
                session.commit()
                
                return {
                    "otp": otp_code,
                    "user_id": str(user.id),
                    "email": user.email,
                    "expires": expires.isoformat()
                }
                
        except Exception as e:
            logger.error(f"Error creating password reset OTP: {str(e)}")
            raise HTTPException(status_code=500, detail="Failed to create password reset OTP")
    
    # Verify password reset OTP
    def verify_password_reset_otp(self, email: str, otp_code: str) -> Dict:
        """
        Verifies a password reset OTP.
        Returns user information if valid.
        """
        try:
            # Validate OTP format
            if not otp_code or len(otp_code) != 6 or not otp_code.isdigit():
                raise HTTPException(
                    status_code=400,
                    detail="Invalid OTP format. OTP must be a 6-digit number."
                )
            
            # Create hash of the OTP to check in database
            otp_hash = hashlib.sha256(f"{otp_code}{email}".encode()).hexdigest()
            
            with self.db.get_session() as session:
                user = session.query(User).filter(User.email == email).first()
                
                if not user:
                    raise HTTPException(
                        status_code=404,
                        detail="User not found"
                    )
                
                # Capture user data within session context
                user_email = user.email
                user_id = str(user.id)
                
                # Check if OTP exists in database and hasn't been used
                token_record = session.query(PasswordResetToken).filter(
                    PasswordResetToken.token_hash == otp_hash,
                    PasswordResetToken.otp_code == otp_code,
                    PasswordResetToken.user_id == user.id,
                    PasswordResetToken.used == False
                ).first()
                
                if not token_record:
                    raise HTTPException(
                        status_code=400,
                        detail="Invalid or expired OTP"
                    )
                
                # Check if OTP has expired
                current_time = datetime.now(timezone.utc)
                expires_at = token_record.expires_at
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=timezone.utc)
                
                if current_time > expires_at:
                    raise HTTPException(
                        status_code=400,
                        detail="OTP has expired"
                    )
            
            # Return result using captured values (outside session context)
            return {
                "email": user_email,
                "user_id": user_id,
                "valid": True
            }
            
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error verifying password reset OTP: {str(e)}")
            raise HTTPException(
                status_code=500,
                detail="Failed to verify password reset OTP"
            )
    
    # Verify password reset token
    def verify_password_reset_token(self, token: str) -> Dict:
        """
        Verifies a password reset token.
        Returns the decoded token data if valid.
        """
        try:
            payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
            
            # Verify token type
            if payload.get("sub") != "password_reset":
                raise HTTPException(
                    status_code=400, 
                    detail="Invalid token type"
                )
            
            # Create hash of the token to check in database
            token_hash = hashlib.sha256(token.encode()).hexdigest()
            
            # Get user and token record from the database
            with self.db.get_session() as session:
                user = session.query(User).filter(User.id == payload["user_id"]).first()
                
                if not user:
                    raise HTTPException(
                        status_code=404,
                        detail="User not found"
                    )
                
                # Check if email matches
                if user.email != payload["email"]:
                    raise HTTPException(
                        status_code=400,
                        detail="Token does not match user email"
                    )
                
                # Check if token exists in database and hasn't been used
                token_record = session.query(PasswordResetToken).filter(
                    PasswordResetToken.token_hash == token_hash,
                    PasswordResetToken.user_id == user.id
                ).first()
                
                if not token_record:
                    raise HTTPException(
                        status_code=400,
                        detail="Invalid password reset token"
                    )
                
                if token_record.used:
                    raise HTTPException(
                        status_code=400,
                        detail="Password reset token has already been used"
                    )
                
                # Check if token has expired (additional check beyond JWT expiration)
                # Ensure both datetimes are timezone-aware for comparison
                current_time = datetime.now(timezone.utc)
                expires_at = token_record.expires_at
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=timezone.utc)
                
                if current_time > expires_at:
                    raise HTTPException(
                        status_code=400,
                        detail="Password reset token has expired"
                    )
            
            return payload
            
        except jwt.ExpiredSignatureError:
            raise HTTPException(
                status_code=400,
                detail="Password reset token has expired"
            )
        except jwt.PyJWTError:
            raise HTTPException(
                status_code=400,
                detail="Invalid password reset token"
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error verifying password reset token: {str(e)}")
            raise HTTPException(
                status_code=500,
                detail="Failed to verify password reset token"
            )
    
    # Reset user password with OTP
    def reset_password_with_otp(self, email: str, otp_code: str, new_password: str) -> bool:
        """
        Resets a user's password using a valid OTP.
        Returns True if successful.
        """
        try:
            # Verify OTP first
            otp_verification = self.verify_password_reset_otp(email, otp_code)
            
            if not otp_verification.get("valid"):
                return False
            
            # Hash the new password
            hashed_password = hash_password(new_password)
            
            # Create hash of the OTP to mark as used
            otp_hash = hashlib.sha256(f"{otp_code}{email}".encode()).hexdigest()
            
            # Update the user's password and mark OTP as used
            with self.db.get_session() as session:
                user = session.query(User).filter(User.email == email).first()
                
                if not user:
                    raise HTTPException(
                        status_code=404,
                        detail="User not found"
                    )
                
                # Update password
                user.hashed_password = hashed_password
                
                # Mark OTP as used
                token_record = session.query(PasswordResetToken).filter(
                    PasswordResetToken.token_hash == otp_hash,
                    PasswordResetToken.otp_code == otp_code,
                    PasswordResetToken.user_id == user.id
                ).first()
                
                if token_record:
                    token_record.used = True
                    token_record.used_at = datetime.now(timezone.utc)
                
                session.commit()
                
                return True
                
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error resetting password with OTP: {str(e)}")
            raise HTTPException(
                status_code=500,
                detail="Failed to reset password"
            )

    def add_work_experience(self, user_id, work_experience_data):
        """
        Add a work experience entry for a user.
        """
        try:
            with self.db.get_session() as session:
                # Verify user exists
                user = session.query(User).filter(User.id == user_id).first()
                if not user:
                    raise HTTPException(status_code=404, detail="User not found")
                
                # Parse dates if provided
                start_date = None
                end_date = None
                
                if work_experience_data.get('start_date'):
                    try:
                        date_parts = work_experience_data['start_date'].split('-')
                        if len(date_parts) == 3:
                            year, month, day = map(int, date_parts)
                            start_date = datetime(year, month, day).date()
                    except (ValueError, AttributeError):
                        raise HTTPException(status_code=400, detail="Invalid start date format. Use YYYY-MM-DD format.")
                
                if work_experience_data.get('end_date'):
                    try:
                        date_parts = work_experience_data['end_date'].split('-')
                        if len(date_parts) == 3:
                            year, month, day = map(int, date_parts)
                            end_date = datetime(year, month, day).date()
                    except (ValueError, AttributeError):
                        raise HTTPException(status_code=400, detail="Invalid end date format. Use YYYY-MM-DD format.")
                
                # Validate that start_date is before end_date if both are provided
                if start_date and end_date and start_date > end_date:
                    raise HTTPException(status_code=400, detail="Start date must be before end date.")
                
                # Create new work experience
                work_experience = WorkExperience(
                    user_id=user_id,
                    company_name=work_experience_data.get('company_name'),
                    achievement=work_experience_data.get('achievement'),
                    start_date=start_date,
                    end_date=end_date
                )
                
                session.add(work_experience)
                session.commit()
                session.refresh(work_experience)
                
                return {
                    "id": str(work_experience.id),
                    "company_name": work_experience.company_name,
                    "achievement": work_experience.achievement,
                    "start_date": work_experience.start_date.isoformat() if work_experience.start_date else None,
                    "end_date": work_experience.end_date.isoformat() if work_experience.end_date else None,
                    "created_at": work_experience.created_at.isoformat() if work_experience.created_at else None,
                    "updated_at": work_experience.updated_at.isoformat() if work_experience.updated_at else None
                }
                
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error adding work experience: {str(e)}")
            raise HTTPException(status_code=500, detail="Internal server error")

    def get_work_experiences(self, user_id):
        """
        Get all work experiences for a user.
        """
        try:
            with self.db.get_session() as session:
                # Verify user exists
                user = session.query(User).filter(User.id == user_id).first()
                if not user:
                    raise HTTPException(status_code=404, detail="User not found")
                
                # Get all work experiences for the user
                work_experiences = session.query(WorkExperience).filter(
                    WorkExperience.user_id == user_id
                ).order_by(WorkExperience.created_at.desc()).all()
                
                return [
                    {
                        "id": str(we.id),
                        "company_name": we.company_name,
                        "achievement": we.achievement,
                        "start_date": we.start_date.isoformat() if we.start_date else None,
                        "end_date": we.end_date.isoformat() if we.end_date else None,
                        "created_at": we.created_at.isoformat() if we.created_at else None,
                        "updated_at": we.updated_at.isoformat() if we.updated_at else None
                    }
                    for we in work_experiences
                ]
                
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error getting work experiences: {str(e)}")
            raise HTTPException(status_code=500, detail="Internal server error")

    def update_work_experience(self, user_id, work_experience_id, work_experience_data):
        """
        Update a work experience entry for a user.
        """
        try:
            with self.db.get_session() as session:
                # Verify user exists
                user = session.query(User).filter(User.id == user_id).first()
                if not user:
                    raise HTTPException(status_code=404, detail="User not found")
                
                # Get the work experience entry
                work_experience = session.query(WorkExperience).filter(
                    WorkExperience.id == work_experience_id,
                    WorkExperience.user_id == user_id
                ).first()
                
                if not work_experience:
                    raise HTTPException(status_code=404, detail="Work experience not found")
                
                # Update fields if provided
                if work_experience_data.get('company_name') is not None:
                    work_experience.company_name = work_experience_data['company_name']
                if work_experience_data.get('achievement') is not None:
                    work_experience.achievement = work_experience_data['achievement']
                
                # Parse and update start_date if provided
                if work_experience_data.get('start_date') is not None:
                    if work_experience_data['start_date']:  # If not empty string
                        try:
                            date_parts = work_experience_data['start_date'].split('-')
                            if len(date_parts) == 3:
                                year, month, day = map(int, date_parts)
                                work_experience.start_date = datetime(year, month, day).date()
                        except (ValueError, AttributeError):
                            raise HTTPException(status_code=400, detail="Invalid start date format. Use YYYY-MM-DD format.")
                    else:
                        work_experience.start_date = None
                
                # Parse and update end_date if provided
                if work_experience_data.get('end_date') is not None:
                    if work_experience_data['end_date']:  # If not empty string
                        try:
                            date_parts = work_experience_data['end_date'].split('-')
                            if len(date_parts) == 3:
                                year, month, day = map(int, date_parts)
                                work_experience.end_date = datetime(year, month, day).date()
                        except (ValueError, AttributeError):
                            raise HTTPException(status_code=400, detail="Invalid end date format. Use YYYY-MM-DD format.")
                    else:
                        work_experience.end_date = None
                
                # Validate that start_date is before end_date if both are provided
                if work_experience.start_date and work_experience.end_date and work_experience.start_date > work_experience.end_date:
                    raise HTTPException(status_code=400, detail="Start date must be before end date.")
                
                session.commit()
                session.refresh(work_experience)
                
                return {
                    "id": str(work_experience.id),
                    "company_name": work_experience.company_name,
                    "achievement": work_experience.achievement,
                    "start_date": work_experience.start_date.isoformat() if work_experience.start_date else None,
                    "end_date": work_experience.end_date.isoformat() if work_experience.end_date else None,
                    "created_at": work_experience.created_at.isoformat() if work_experience.created_at else None,
                    "updated_at": work_experience.updated_at.isoformat() if work_experience.updated_at else None
                }
                
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error updating work experience: {str(e)}")
            raise HTTPException(status_code=500, detail="Internal server error")

    def delete_work_experience(self, user_id, work_experience_id):
        """
        Delete a work experience entry for a user.
        """
        try:
            with self.db.get_session() as session:
                # Verify user exists
                user = session.query(User).filter(User.id == user_id).first()
                if not user:
                    raise HTTPException(status_code=404, detail="User not found")
                
                # Get the work experience entry
                work_experience = session.query(WorkExperience).filter(
                    WorkExperience.id == work_experience_id,
                    WorkExperience.user_id == user_id
                ).first()
                
                if not work_experience:
                    raise HTTPException(status_code=404, detail="Work experience not found")
                
                session.delete(work_experience)
                session.commit()
                
                return {"message": "Work experience deleted successfully"}
                
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error deleting work experience: {str(e)}")
            raise HTTPException(status_code=500, detail="Internal server error")

    def get_legal_services(self):
        """
        Get the list of available legal services.
        """
        return LEGAL_SERVICES

    def get_legal_skills(self):
        """
        Get the list of available legal skills.
        """
        return LEGAL_SKILLS

    def _update_work_experiences(self, session, user_id, work_experiences_data):
        """
        Update work experiences for a user.
        This will create new ones, update existing ones, and delete ones not included.
        """
        try:
            # Get existing work experiences for the user
            existing_work_experiences = session.query(WorkExperience).filter(
                WorkExperience.user_id == user_id
            ).all()
            
            # Create a dictionary to map existing work experience IDs to their objects
            existing_work_experience_dict = {str(we.id): we for we in existing_work_experiences}
            updated_work_experience_ids = set()
            
            for work_experience_data in work_experiences_data:
                work_experience_id = work_experience_data.get('id')
                
                # Parse dates
                start_date = None
                end_date = None
                
                if work_experience_data.get('start_date'):
                    try:
                        date_parts = work_experience_data['start_date'].split('-')
                        if len(date_parts) == 3:
                            year, month, day = map(int, date_parts)
                            start_date = datetime(year, month, day).date()
                    except (ValueError, AttributeError):
                        raise HTTPException(status_code=400, detail="Invalid start date format. Use YYYY-MM-DD format.")
                
                if work_experience_data.get('end_date'):
                    try:
                        date_parts = work_experience_data['end_date'].split('-')
                        if len(date_parts) == 3:
                            year, month, day = map(int, date_parts)
                            end_date = datetime(year, month, day).date()
                    except (ValueError, AttributeError):
                        raise HTTPException(status_code=400, detail="Invalid end date format. Use YYYY-MM-DD format.")
                
                # Validate that start_date is before end_date if both are provided
                if start_date and end_date and start_date > end_date:
                    raise HTTPException(status_code=400, detail="Start date must be before end date.")
                
                if work_experience_id and work_experience_id in existing_work_experience_dict:
                    # Update existing work experience
                    work_experience = existing_work_experience_dict[work_experience_id]
                    work_experience.company_name = work_experience_data['company_name']
                    work_experience.achievement = work_experience_data.get('achievement')
                    work_experience.start_date = start_date
                    work_experience.end_date = end_date
                    updated_work_experience_ids.add(work_experience_id)
                else:
                    # Create new work experience
                    work_experience = WorkExperience(
                        user_id=user_id,
                        company_name=work_experience_data['company_name'],
                        achievement=work_experience_data.get('achievement'),
                        start_date=start_date,
                        end_date=end_date
                    )
                    session.add(work_experience)
            
            # Delete work experiences that were not included in the update
            for existing_id, existing_work_experience in existing_work_experience_dict.items():
                if existing_id not in updated_work_experience_ids:
                    session.delete(existing_work_experience)
            
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error updating work experiences: {str(e)}")
            raise HTTPException(status_code=500, detail="Internal server error")

    # Email verification methods
    def create_email_verification_token(self, email: str, registration_data: Dict) -> Dict:
        from .registration_security import issue
        try:
            return issue(self.db, email, registration_data)
        except SQLAlchemyError:
            logger.error("Pending registration storage failed")
            raise HTTPException(500, "Failed to start registration") from None

    def resend_email_verification_token(self, email: str) -> Dict:
        from .registration_security import issue
        try:
            return issue(self.db, email)
        except SQLAlchemyError:
            logger.error("Pending registration resend storage failed")
            raise HTTPException(500, "Failed to resend verification code") from None

    def complete_user_registration(self, email: str, otp_code: str) -> Dict:
        from .registration_security import complete
        try:
            return complete(self.db, email, otp_code)
        except IntegrityError:
            raise HTTPException(409, "Registration conflicts with an existing account") from None
        except SQLAlchemyError:
            logger.error("Registration completion storage failed")
            raise HTTPException(500, "Failed to complete registration") from None

    def delete_user_account(self, user_id):
        """
        Delete a user account and all associated data.
        This method handles:
        1. Deleting S3 files (profile picture, proof of ID)
        2. Deleting user from database (CASCADE will handle related records)
        """
        try:
            # Create S3 client for file cleanup
            s3_client = boto3.client(
                's3',
                aws_access_key_id=os.getenv('AWS_ACCESS_KEY_ID'),
                aws_secret_access_key=os.getenv('AWS_SECRET_ACCESS_KEY'),
                region_name=os.getenv('AWS_REGION', 'us-east-1')
            )
            
            bucket_name = os.getenv('S3_BUCKET_NAME', 'lawneeds-profile-pictures')
            
            with self.db.get_session() as session:
                # Get user to check if they exist and get file paths
                user = session.query(User).filter(User.id == user_id).first()
                
                if not user:
                    raise HTTPException(status_code=404, detail="User not found")
                
                user_client = user.client  # Store for S3 cleanup
                
                # Clean up S3 files
                files_to_delete = []
                
                # Add profile picture if exists
                if user.profile_picture_path and user.profile_picture_path.startswith(f"https://{bucket_name}.s3.amazonaws.com/"):
                    s3_key = user.profile_picture_path.replace(f"https://{bucket_name}.s3.amazonaws.com/", "")
                    files_to_delete.append(s3_key)
                
                # Add proof of ID if exists
                if user.proof_of_id and user.proof_of_id.startswith(f"https://{bucket_name}.s3.amazonaws.com/"):
                    s3_key = user.proof_of_id.replace(f"https://{bucket_name}.s3.amazonaws.com/", "")
                    files_to_delete.append(s3_key)
                
                # Delete all S3 files for this user (in case there are other files)
                # This will delete all files in the user's client folder
                try:
                    # List all objects with the user's client prefix
                    paginator = s3_client.get_paginator('list_objects_v2')
                    pages = paginator.paginate(Bucket=bucket_name, Prefix=f"profile_pictures/{user_client}/")
                    
                    for page in pages:
                        if 'Contents' in page:
                            for obj in page['Contents']:
                                files_to_delete.append(obj['Key'])
                    
                    # Also check proof_of_id folder
                    pages = paginator.paginate(Bucket=bucket_name, Prefix=f"proof_of_id/{user_client}/")
                    for page in pages:
                        if 'Contents' in page:
                            for obj in page['Contents']:
                                files_to_delete.append(obj['Key'])
                                
                except Exception as s3_error:
                    logger.warning(f"Could not list S3 objects for user {user_id}: {str(s3_error)}")
                
                # Delete all identified files from S3
                for file_key in set(files_to_delete):  # Use set to avoid duplicates
                    try:
                        s3_client.delete_object(Bucket=bucket_name, Key=file_key)
                        logger.info(f"Deleted S3 file: {file_key}")
                    except Exception as delete_error:
                        logger.warning(f"Could not delete S3 file {file_key}: {str(delete_error)}")
                
                # ------------------------------------------------------------------
                # Pre-delete: clear FK columns that have no ON DELETE rule and
                # would cause a constraint violation when the user row is removed.
                # ------------------------------------------------------------------

                # 1. referral_requests.matched_lawyer_id → users.id (no ondelete)
                #    Blocks deletion when this user is a lawyer matched to referrals.
                matched_referrals = (
                    session.query(ReferralRequest)
                    .filter(ReferralRequest.matched_lawyer_id == user_id)
                    .all()
                )
                for ref in matched_referrals:
                    ref.matched_lawyer_id = None
                    ref.status = "unmatched"
                if matched_referrals:
                    logger.info(
                        f"Cleared matched_lawyer_id on {len(matched_referrals)} "
                        f"referral request(s) before deleting user {user_id}"
                    )

                # 2. annual_consultation_credits.referral_id → referral_requests.id (no ondelete)
                #    Blocks CASCADE deletion of this user's referral_requests rows.
                user_referral_ids = [
                    r.id
                    for r in session.query(ReferralRequest.id)
                    .filter(ReferralRequest.user_id == user_id)
                    .all()
                ]
                if user_referral_ids:
                    credits_cleared = (
                        session.query(AnnualConsultationCredit)
                        .filter(AnnualConsultationCredit.referral_id.in_(user_referral_ids))
                        .all()
                    )
                    for credit in credits_cleared:
                        credit.referral_id = None
                    if credits_cleared:
                        logger.info(
                            f"Cleared referral_id on {len(credits_cleared)} "
                            f"annual_consultation_credit row(s) before deleting user {user_id}"
                        )

                session.flush()

                # Delete user from database
                # Due to CASCADE constraints, this will automatically delete:
                # - WorkExperience records
                # - ReferralRequest records (and related ReferralSession, LawyerRecommendation, LawyerInbox)
                # - LawyerAvailability records
                # - InboxMessage records
                # - user_knowledgebase associations
                from .registration_security import clear_registration
                clear_registration(session, user.email)
                session.delete(user)
                session.commit()
                
                logger.info(f"Successfully deleted user account: {user_id}")
                return {
                    "message": "Account deleted successfully",
                    "user_id": str(user_id),
                    "files_deleted": len(files_to_delete)
                }
                
        except HTTPException:
            raise
        except ClientError as e:
            logger.error(f"S3 error during user deletion: {str(e)}")
            # Continue with database deletion even if S3 fails
            try:
                with self.db.get_session() as session:
                    user = session.query(User).filter(User.id == user_id).first()
                    if user:
                        # Clear blocking FKs before delete (same as main path)
                        matched_referrals = (
                            session.query(ReferralRequest)
                            .filter(ReferralRequest.matched_lawyer_id == user_id)
                            .all()
                        )
                        for ref in matched_referrals:
                            ref.matched_lawyer_id = None
                            ref.status = "unmatched"

                        user_referral_ids = [
                            r.id
                            for r in session.query(ReferralRequest.id)
                            .filter(ReferralRequest.user_id == user_id)
                            .all()
                        ]
                        if user_referral_ids:
                            for credit in (
                                session.query(AnnualConsultationCredit)
                                .filter(AnnualConsultationCredit.referral_id.in_(user_referral_ids))
                                .all()
                            ):
                                credit.referral_id = None

                        session.flush()
                        from .registration_security import clear_registration
                        clear_registration(session, user.email)
                        session.delete(user)
                        session.commit()
                        return {
                            "message": "Account deleted successfully (some files may remain in S3)",
                            "user_id": str(user_id),
                            "s3_warning": "Could not clean up all S3 files"
                        }
            except Exception as db_error:
                logger.error(f"Database error during user deletion: {str(db_error)}")
                raise HTTPException(status_code=500, detail="Failed to delete account")
        except Exception as e:
            logger.error(f"Error deleting user account: {str(e)}")
            raise HTTPException(status_code=500, detail="Failed to delete account")

    def delete_user_by_id_backend(self, user_id_str):
        """
        Backend-only method to delete a user by ID string.
        This method is for admin/backend use only.
        """
        try:
            # Convert string ID to UUID
            try:
                if isinstance(user_id_str, str):
                    user_id = uuid.UUID(user_id_str)
                else:
                    user_id = user_id_str
            except ValueError:
                raise HTTPException(status_code=400, detail="Invalid user ID format")
            
            # Use the same deletion logic
            return self.delete_user_account(user_id)
            
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error in backend user deletion: {str(e)}")
            raise HTTPException(status_code=500, detail="Failed to delete user")

    def get_all_pro_users(self):
        """
        Get all professional users (Lawyers, Law Firms, A2J Partners, HR/Legal SME) 
        for display to normal users.
        Returns complete profile information for each user.
        """
        try:
            with self.db.get_session() as session:
                # Query users with professional types
                pro_users = session.query(User).filter(
                    User.professional_type.in_([
                        "Lawyer",
                        "Law Firm"
                    ])
                ).all()
                
                # Get complete profile for each professional user
                pro_user_profiles = []
                for user in pro_users:
                    # Use the existing get_user_profile method to get complete profile
                    profile = self.get_user_profile(str(user.id))
                    pro_user_profiles.append(profile)
                
                return pro_user_profiles
                
        except Exception as e:
            logger.error(f"Error getting professional users: {str(e)}")
            raise HTTPException(status_code=500, detail="Failed to get professional users")
