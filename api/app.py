from fastapi import HTTPException, APIRouter, FastAPI, Request, Query, File, UploadFile, Body, Form, Depends, BackgroundTasks, Header
from fastapi.responses import JSONResponse
from typing import Optional, List, Dict, Any
import threading
from datetime import datetime, timedelta, timezone
import traceback
from pydantic import BaseModel, EmailStr, Field
import logging
import mimetypes
import os
from enum import Enum
import boto3
import uuid
from sqlalchemy import or_, func, cast, String, literal
from services.models import (
    User,
    ReferralRequest,
    ReferralStatus,
    LawyerRecommendation,
    LawyerInbox,
    InboxMessage,
    ConsultationBookingRequest,
    DeviceToken,
    ConsultationType,
    LawyerAvailability,
    ProfessionalPresence,
)
from services.booking_service import (
    BookingService,
    consultation_duration_minutes,
    effective_presence_status,
    ensure_utc,
    parse_datetime,
    requires_booking_slot,
)
from services.upload_multipart import (
    merge_referral_upload_into_context,
    build_referral_attachment_metadata,
    infer_referral_file_type,
    try_pdf_document_title,
    content_sha256_hex,
    check_duplicate_filename,
)
from services.referral_consultation_pricing_display import (
    casework_budget_options,
    casework_service_options,
    CASEWORK_MIN_BUDGET,
    CASEWORK_PRICE_BY_SERVICE,
    CASEWORK_LABEL_BY_SERVICE,
    CASEWORK_SERVICE_VALUES,
    validate_casework_budget,
)
from api.admin import AdminAPI
from services.review_service import ReviewService
from services.profile_taxonomy import (
    resolve_practice_filter, resolve_service_filters,
    search_expertise_payload, labels_to_service_items,
    get_practice_area_catalog, get_service_catalog, get_skill_catalog,
)
from services.firebase_notification_service import FirebaseNotificationService
from services.notification_service import NotificationService
from services.pdf_service import PDFService
from services.referral_display_helpers import (
    get_budget_range_display,
    get_consultation_duration_display,
    get_consultation_type_display,
    get_urgency_level_display,
)
from services.s3_service import S3Service
from services.lawyer_response_stats import get_lawyer_response_stats
from services.persona_service import latest_persona_verification_approved_clause

# Import subscription service for usage validation
try:
    from services.subscription_service import SubscriptionService
    from services.subscription_constants import UsageActionType, BlockedReason
    SUBSCRIPTION_FEATURES_ENABLED = True
except ImportError as e:
    logging.warning(f"Subscription features not available for app.py: {e}")
    SUBSCRIPTION_FEATURES_ENABLED = False

# Initialize the client id and lock
client_id_counter = 0
client_id_lock = threading.Lock()

# Set up logging
logger = logging.getLogger(__name__)


def validate_request_service_budget_range(
    consultation_category: str,
    consultation_type: str,
    budget_range: Optional[str],
) -> None:
    """Validate non-casework request-service budgets after category-specific checks."""
    if not budget_range or consultation_category == "casework":
        return

    valid_3_day_budgets = ["300", "350", "400", "450", "500"]
    valid_5_day_budgets = ["500", "550", "600", "650", "700"]
    valid_range_budgets = [
        "below_300",
        "300_to_499",
        "300_to_500",
        "500_to_699",
        "500_to_700",
        "above_700",
    ]

    if consultation_type == "ongoing_3_day":
        if budget_range not in valid_3_day_budgets and budget_range not in ["300_to_499", "300_to_500"]:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Invalid budget_range for 3-day support. Must be one of: "
                    f"{', '.join(valid_3_day_budgets)} or '300_to_499'"
                ),
            )
    elif consultation_type == "ongoing_5_day":
        if budget_range not in valid_5_day_budgets and budget_range not in ["500_to_699", "500_to_700"]:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Invalid budget_range for 5-day support. Must be one of: "
                    f"{', '.join(valid_5_day_budgets)} or '500_to_699'"
                ),
            )
    elif budget_range not in valid_range_budgets:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid budget_range. Must be one of: {', '.join(valid_range_budgets)}",
        )


# Define enums for dropdown selections
class ProfessionalType(str, Enum):
    LAWYER = "Lawyer"
    LAW_FIRM = "Law Firm"
    HR_LEGAL = "HR/Legal (SME)"
    SME = "SME"
    A2J_PARTNERS = "A2J Partners"
    
class RegulatoryBody(str, Enum):
    SRA = "SRA"
    BSB = "BSB"
    IAA = "IAA"  # Added Immigration Advisers Authority
    NONE = "None"
    
class HRLegalType(str, Enum):
    HR = "HR"
    LEGAL = "Legal"
    BOTH = "Both"

class A2JOrganisationType(str, Enum):
    LEGAL_AID = "Legal Aid Organisation"
    LAW_CENTRE = "Law Centre"
    PRO_BONO = "Pro-Bono Clinic"
    
class PracticeArea(str, Enum):
    IMMIGRATION = "Immigration law"
    EMPLOYMENT = "Employment law"
    TRAFFIC = "Road Traffic Offences"
    FAMILY = "Family law"

class UserLogin(BaseModel):
    email: str
    password: str

class ProfileUpdate(BaseModel):
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    address: Optional[str] = None
    town_city: Optional[str] = None
    country: Optional[str] = None
    postcode: Optional[str] = None
    # Note: username, email, and password are intentionally excluded

class LawyerProfileUpdate(BaseModel):
    # Basic fields
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    address: Optional[str] = None
    town_city: Optional[str] = None
    country: Optional[str] = None
    postcode: Optional[str] = None
    # Lawyer specific fields
    sra_bsb_iaa: Optional[str] = None
    sra_bsb: Optional[RegulatoryBody] = None
    professional_employer: Optional[str] = None
    legal_expertise: Optional[List[str]] = None

class LawFirmProfileUpdate(BaseModel):
    # Basic fields
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    address: Optional[str] = None
    town_city: Optional[str] = None
    country: Optional[str] = None
    postcode: Optional[str] = None
    # Law Firm specific fields
    firm_name: Optional[str] = None
    sra_bsb: Optional[RegulatoryBody] = None
    sra_bsb_iaa: Optional[str] = None
    practice_areas: Optional[List[str]] = None
    contact_person: Optional[str] = None
    company_registration_number: Optional[str] = None
    services: Optional[List[str]] = None

class HRLegalProfileUpdate(BaseModel):
    # Basic fields
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    address: Optional[str] = None
    town_city: Optional[str] = None
    country: Optional[str] = None
    postcode: Optional[str] = None
    # HR/Legal specific fields
    hr_legal_type: Optional[HRLegalType] = None
    sector: Optional[str] = None
    company_name: Optional[str] = None
    company_registration_number: Optional[str] = None
    sra_bsb_iaa: Optional[str] = None
    sra_bsb: Optional[RegulatoryBody] = None
    services: Optional[List[str]] = None
    skills: Optional[List[str]] = None

class A2JPartnersProfileUpdate(BaseModel):
    # Basic fields
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    address: Optional[str] = None
    town_city: Optional[str] = None
    country: Optional[str] = None
    postcode: Optional[str] = None
    # A2J Partners specific fields
    organisation_name: Optional[str] = None
    date_of_establishment: Optional[str] = None  # Will be processed as date
    organisation_type: Optional[A2JOrganisationType] = None
    areas_of_legal_focus: Optional[List[str]] = None
    contact_person: Optional[str] = None
    registration_charity_number: Optional[str] = None
    sra_bsb_iaa: Optional[str] = None
    sra_bsb: Optional[RegulatoryBody] = None
    services: Optional[List[str]] = None
    skills: Optional[List[str]] = None

class WorkExperienceProfile(BaseModel):
    id: Optional[str] = None  # If provided, updates existing; if None, creates new
    company_name: str
    achievement: Optional[str] = None
    start_date: Optional[str] = None  # Expected format: YYYY-MM-DD
    end_date: Optional[str] = None    # Expected format: YYYY-MM-DD, null for current positions

class WorkExperienceCreate(BaseModel):
    company_name: str
    achievement: Optional[str] = None
    start_date: Optional[str] = None  # Expected format: YYYY-MM-DD
    end_date: Optional[str] = None    # Expected format: YYYY-MM-DD, null for current positions

class WorkExperienceUpdate(BaseModel):
    company_name: Optional[str] = None
    achievement: Optional[str] = None
    start_date: Optional[str] = None  # Expected format: YYYY-MM-DD
    end_date: Optional[str] = None    # Expected format: YYYY-MM-DD, null for current positions

class UnifiedProfileUpdate(BaseModel):
    # Common fields for all user types
    email: Optional[EmailStr] = None
    user_name: Optional[str] = Field(None, min_length=3, max_length=80)
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    date_of_birth: Optional[str] = None
    address: Optional[str] = None
    town_city: Optional[str] = None
    country: Optional[str] = None
    postcode: Optional[str] = None
    
    # New fields for all users
    about: Optional[str] = None
    tagline: Optional[str] = None
    phone_number: Optional[str] = None
    immigration_status: Optional[str] = Field(None, max_length=120)
    webinar: Optional[bool] = None
    
    # Work experiences for all users
    work_experiences: Optional[List[WorkExperienceProfile]] = None
    
    # Lawyer specific fields
    sra_bsb_iaa: Optional[str] = None
    sra_bsb: Optional[RegulatoryBody] = None
    professional_employer: Optional[str] = None
    legal_expertise: Optional[List[str]] = None
    legal_expertise_ids: Optional[List[int]] = Field(None, description="Practice area IDs (preferred over string names)")
    services: Optional[List[str]] = None
    service_ids: Optional[List[int]] = Field(None, description="Legal service IDs (preferred over string names)")
    skills: Optional[List[str]] = None
    skill_ids: Optional[List[int]] = Field(None, description="Legal skill IDs (preferred over string names)")
    hourly_rate: Optional[float] = Field(None, ge=0, description="Hourly rate in pounds (£)")
    
    # Law Firm specific fields
    firm_name: Optional[str] = None
    practice_areas: Optional[List[str]] = None
    practice_area_ids: Optional[List[int]] = Field(None, description="Practice area IDs (preferred over string names)")
    contact_person: Optional[str] = None
    company_registration_number: Optional[str] = None
    firm_address: Optional[str] = None
    firm_website: Optional[str] = None
    firm_phone_number: Optional[str] = None
    firm_hourly_rate: Optional[float] = Field(None, ge=0, description="Hourly rate in pounds (£) for law firms")
    
    # HR/Legal specific fields
    hr_legal_type: Optional[HRLegalType] = None
    sector: Optional[str] = None
    company_name: Optional[str] = None
    
    # A2J Partners specific fields
    organisation_name: Optional[str] = None
    date_of_establishment: Optional[str] = None
    organisation_type: Optional[A2JOrganisationType] = None
    areas_of_legal_focus: Optional[List[str]] = None
    areas_of_legal_focus_ids: Optional[List[int]] = Field(None, description="Practice area IDs (preferred over string names)")
    registration_charity_number: Optional[str] = None
    a2j_website: Optional[str] = None


class AvailabilityRulePayload(BaseModel):
    weekday: int = Field(..., ge=0, le=6, description="Monday=0, Sunday=6")
    start_minute: int = Field(..., ge=0, le=1439)
    end_minute: int = Field(..., ge=1, le=1440)
    timezone: str = "Europe/London"
    consultation_types: Optional[List[str]] = None
    is_active: bool = True


class AvailabilityExceptionPayload(BaseModel):
    start_at: str
    end_at: str
    timezone: str = "Europe/London"
    is_available: bool = False
    consultation_types: Optional[List[str]] = None
    reason: Optional[str] = None


class AvailabilityPayload(BaseModel):
    rules: List[AvailabilityRulePayload] = Field(default_factory=list)
    exceptions: List[AvailabilityExceptionPayload] = Field(default_factory=list)


class PresencePayload(BaseModel):
    manual_status_override: Optional[str] = Field(
        None,
        description="Use away, offline, or empty string to clear the override.",
    )
    accepting_consultations: Optional[bool] = None
    heartbeat: bool = True


class AppAPI:
    
    # Init.
    def __init__(self, *, app_name, db, auth):
        self.app_name = app_name
        self.db = db
        self.auth = auth
        self.review_service = ReviewService(db)
        self.firebase_service = FirebaseNotificationService()
        self.pdf_service = PDFService()
        self.s3_service = S3Service()

    def _normalize_budget_range(self, budget_range: str) -> str:
        """
        Convert specific budget amounts to appropriate range categories for storage.
        This ensures compatibility with the existing range-based enum system.
        """
        if not budget_range:
            return budget_range
            
        # Canonical and variant spellings — always normalise to canonical values
        _canonical = {
            "below_300":  "below_300",
            "300_to_499": "300_to_499",
            "300_to_500": "300_to_499",
            "500_to_699": "500_to_699",
            "500_to_700": "500_to_699",
            "above_700":  "above_700",
        }
        if budget_range in _canonical:
            return _canonical[budget_range]

        # Convert specific amounts to canonical ranges
        try:
            amount = int(budget_range)
            if amount < 300:
                return "below_300"
            elif 300 <= amount <= 499:
                return "300_to_499"
            elif 500 <= amount <= 700:
                return "500_to_699"
            else:
                return "above_700"
        except ValueError:
            return budget_range

    def _ongoing_budget_gbp(self, consultation_type: Optional[str], budget_range: Optional[str]) -> Optional[int]:
        if not consultation_type or not budget_range:
            return None

        valid_budgets = {
            "ongoing_3_day": {300, 350, 400, 450, 500},
            "ongoing_5_day": {500, 550, 600, 650, 700},
        }.get(consultation_type)
        if not valid_budgets:
            return None

        try:
            amount = int(str(budget_range).strip())
        except (TypeError, ValueError):
            return None

        return amount if amount in valid_budgets else None
    
    def _get_user_push_token(self, session, user_id: str) -> Optional[str]:
        """
        Get the primary (most recently used) active push token for a user.
        Returns None if no active tokens are found.
        """
        try:
            device = session.query(DeviceToken).filter(
                DeviceToken.user_id == uuid.UUID(user_id),
                DeviceToken.is_active == True
            ).order_by(DeviceToken.last_used_at.desc()).first()
            
            return device.device_token if device else None
        except Exception as e:
            logger.warning(f"Error fetching push token for user {user_id}: {e}")
            return None

    def _require_professional(self, session, user_id: str) -> User:
        try:
            user_uuid = uuid.UUID(str(user_id))
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid user ID in token")
        professional = session.query(User).filter(
            User.id == user_uuid,
            User.professional_type.in_(["Lawyer", "Law Firm", "A2J Partners", "HR/Legal (SME)"]),
        ).first()
        if not professional:
            raise HTTPException(status_code=403, detail="Professional account required")
        return professional

    def _availability_summary(self, session, professional_id: uuid.UUID) -> Dict[str, Any]:
        booking_service = BookingService(session)
        presence = session.query(ProfessionalPresence).filter(
            ProfessionalPresence.professional_id == professional_id
        ).first()
        legacy_availability = session.query(LawyerAvailability).filter(
            LawyerAvailability.lawyer_id == professional_id
        ).first()
        accepting = True
        if legacy_availability is not None and legacy_availability.is_available is False:
            accepting = False
        status, label = effective_presence_status(
            presence,
            accepting_consultations=accepting,
        )
        try:
            next_available = booking_service.next_available_at(professional_id)
        except Exception as exc:
            logger.debug("Could not calculate next availability for %s: %s", professional_id, exc)
            next_available = None
        return {
            "availability_status": status,
            "availability_label": label,
            "next_available_at": next_available.isoformat() if next_available else None,
        }

    # Creates a db engine.
    def get_router(self):
        
        router = APIRouter()
        
        ############################################
        # Test                                     #
        ############################################
        @router.get("/test")
        async def test():
            try:
                return {"response": "test " + self.app_name}
            except Exception as e:
                print(e)
                raise HTTPException(status_code=500, detail="An unexpected error occurred")

        @router.get("/casework/services")
        async def get_casework_services():
            """Return casework service options and price ranges for frontend pickers."""
            return [
                {
                    "key": option["value"],
                    "label": option["label"],
                    "service_label": option["service_label"],
                    "price_range": option["price_range"],
                    "min_budget_gbp": option["min_budget_gbp"],
                    "max_budget_gbp": option["max_budget_gbp"],
                }
                for option in casework_service_options()
            ]

        @router.get("/casework/budget-options")
        async def get_casework_budget_options(
            service: str = Query(..., description="Casework service key, e.g. skilled_worker_visa")
        ):
            """Return allowed budget options for the selected casework service."""
            if service not in CASEWORK_MIN_BUDGET:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Unknown casework service. Must be one of: "
                        + ", ".join(CASEWORK_SERVICE_VALUES)
                    ),
                )

            return {
                "casework_budget": [
                    {
                        "key": option["value"],
                        "label": option["label"],
                    }
                    for option in casework_budget_options(service)
                ]
            }
            
        ############################################
        # Create User                              #
        ############################################

        @router.post("/create_user")
        async def create_user(
            background_tasks: BackgroundTasks,
            # The streamlined client signup only asks for these required fields.
            first_name: str = Form(..., min_length=2, description="User's first name"),
            last_name: str = Form(..., min_length=2, description="User's last name"),
            email: EmailStr = Form(..., description="User's email"),
            password: str = Form(..., min_length=8, description="User's password"),
            day_of_birth: int = Form(..., ge=1, le=31, description="Day of birth"),
            month_of_birth: int = Form(..., ge=1, le=12, description="Month of birth"),
            year_of_birth: int = Form(..., description="Year of birth"),
            
            # Deferred profile fields. Older clients may still send them.
            country: Optional[str] = Form(None, description="User's country"),
            user_name: Optional[str] = Form(None, description="User's username"),
            address: Optional[str] = Form(None, description="User's address"),
            town_city: Optional[str] = Form(None, description="User's town/city"),
            postcode: Optional[str] = Form(None, description="User's postcode")
        ):
            try:
                normalized_email = str(email).strip().lower()
                first_name = first_name.strip()
                last_name = last_name.strip()
                country = country.strip() if country and country.strip() else None

                if len(first_name) < 2 or len(last_name) < 2:
                    raise HTTPException(
                        status_code=400,
                        detail="First name and last name must each contain at least 2 characters",
                    )

                try:
                    date_of_birth = datetime(year_of_birth, month_of_birth, day_of_birth).date()
                except ValueError as e:
                    raise HTTPException(status_code=400, detail=f"Invalid date of birth: {str(e)}")

                if date_of_birth >= datetime.now(timezone.utc).date():
                    raise HTTPException(status_code=400, detail="Date of birth must be in the past")

                resolved_user_name = (
                    user_name.strip()
                    if user_name and user_name.strip()
                    else self.auth.generate_unique_username(first_name, last_name, normalized_email)
                )
                
                # Generate client ID
                global client_id_counter
                with client_id_lock:
                    client_id_counter += 1
                    client = str(client_id_counter)
                
                # Prepare registration data for email verification
                registration_data = {
                    "user_name": resolved_user_name,
                    "email": normalized_email,
                    "password": password,
                    "client": client,
                    "first_name": first_name,
                    "last_name": last_name,
                    "date_of_birth": date_of_birth.isoformat() if date_of_birth else None,
                    "address": address,
                    "town_city": town_city,
                    "country": country,
                    "postcode": postcode,
                    "professional_type": None
                }
                
                # Create email verification token and send OTP
                from services.email import EmailService
                otp_data = self.auth.create_email_verification_token(normalized_email, registration_data)
                
                # Send verification email in background
                background_tasks.add_task(
                    EmailService.send_email_verification_email,
                    normalized_email,
                    otp_data["otp"]
                )
                
                return {
                    "message": "Registration initiated. Please check your email for verification code.",
                    "email": normalized_email,
                    "expires_in": "15 minutes",
                    "next_step": "verify_email"
                }
            
            except HTTPException as he:
                # Re-raise HTTP exceptions
                raise he
            except Exception as e:
                print(e)
                traceback.print_exc()
                raise HTTPException(status_code=500, detail="An unexpected error occurred")
        
        # Create a dedicated endpoint for professional user registration
        @router.post("/create_pro_account")
        async def create_pro_account(
            background_tasks: BackgroundTasks,
            # Professional user registration with email verification
            professional_type: ProfessionalType = Form(..., description="Type of professional user"),
            # Include all other parameters from create_user endpoint
            first_name: Optional[str] = Form(None, description="User's first name"),
            last_name: Optional[str] = Form(None, description="User's last name"),
            user_name: str = Form(..., description="User's username"),
            email: str = Form(..., description="User's email"),
            password: str = Form(..., description="User's password"),
            address: str = Form(..., description="User's address"),
            town_city: str = Form(..., description="User's town/city"),
            country: str = Form(..., description="User's country"),
            postcode: str = Form(..., description="User's postcode"),
            
            # Conditional fields based on professional_type
            # Lawyer specific fields
            day_of_birth: Optional[int] = Form(None, ge=1, le=31, description="Day of birth"),
            month_of_birth: Optional[int] = Form(None, ge=1, le=12, description="Month of birth"),
            year_of_birth: Optional[int] = Form(None, description="Year of birth"),
            sra_bsb_iaa: Optional[str] = Form(None, description="SRA/BSB/IAA license number"),
            professional_employer: Optional[str] = Form(None, description="Professional employer"),
            legal_expertise: Optional[str] = Form(None, description="Legal expertise areas (comma-separated)"),
            
            # Law Firm specific fields
            firm_name: Optional[str] = Form(None, description="Firm name"),
            day_of_incorporation: Optional[int] = Form(None, ge=1, le=31, description="Day of incorporation"),
            month_of_incorporation: Optional[int] = Form(None, ge=1, le=12, description="Month of incorporation"),
            year_of_incorporation: Optional[int] = Form(None, description="Year of incorporation"),
            sra_bsb: Optional[RegulatoryBody] = Form(None, description="SRA/BSB/IAA regulatory body"),
            practice_areas: Optional[str] = Form(None, description="Practice areas (comma-separated or single value)"),
            contact_person: Optional[str] = Form(None, description="Contact person/Managing partner"),
            company_registration_number: Optional[str] = Form(None, description="Company registration number"),
            
            # HR/Legal (SME) specific fields
            hr_legal_type: Optional[HRLegalType] = Form(None, description="HR/Legal/Both"),
            sector: Optional[str] = Form(None, description="Sector"),
            company_name: Optional[str] = Form(None, description="Company name"),
            
            # A2J Partners specific fields
            organisation_name: Optional[str] = Form(None, description="Organisation name"),
            day_of_establishment: Optional[int] = Form(None, ge=1, le=31, description="Day of establishment"),
            month_of_establishment: Optional[int] = Form(None, ge=1, le=12, description="Month of establishment"),
            year_of_establishment: Optional[int] = Form(None, description="Year of establishment"),
            organisation_type: Optional[A2JOrganisationType] = Form(None, description="Organisation type"),
            areas_of_legal_focus: Optional[str] = Form(None, description="Areas of legal focus (comma-separated)"),
            registration_charity_number: Optional[str] = Form(None, description="Registration/Charity number")
        ):
            try:
                if professional_type == ProfessionalType.SME:
                    professional_type = ProfessionalType.HR_LEGAL

                # Validate first_name and last_name based on professional type
                if professional_type in [ProfessionalType.LAWYER, ProfessionalType.HR_LEGAL]:
                    if not first_name:
                        raise HTTPException(status_code=400, detail="First name is required for individual professional accounts")
                    if not last_name:
                        raise HTTPException(status_code=400, detail="Last name is required for individual professional accounts")
                
                # Process date of birth if provided
                date_of_birth = None
                if day_of_birth and month_of_birth and year_of_birth:
                    try:
                        date_of_birth = datetime(year_of_birth, month_of_birth, day_of_birth).date()
                    except ValueError as e:
                        raise HTTPException(status_code=400, detail=f"Invalid date of birth: {str(e)}")
                
                # Process date of incorporation if provided
                date_of_incorporation = None
                if professional_type in [ProfessionalType.LAW_FIRM, ProfessionalType.HR_LEGAL] and day_of_incorporation and month_of_incorporation and year_of_incorporation:
                    try:
                        date_of_incorporation = datetime(year_of_incorporation, month_of_incorporation, day_of_incorporation).date()
                    except ValueError as e:
                        raise HTTPException(status_code=400, detail=f"Invalid date of incorporation: {str(e)}")
                
                # Process date of establishment for A2J Partners
                date_of_establishment = None
                if professional_type == ProfessionalType.A2J_PARTNERS and day_of_establishment and month_of_establishment and year_of_establishment:
                    try:
                        date_of_establishment = datetime(year_of_establishment, month_of_establishment, day_of_establishment).date()
                    except ValueError as e:
                        raise HTTPException(status_code=400, detail=f"Invalid date of establishment: {str(e)}")
                
                # Validate required fields based on professional type
                if professional_type == ProfessionalType.LAWYER:
                    if not sra_bsb_iaa:
                        raise HTTPException(status_code=400, detail="SRA/BSB/IAA is required for Lawyer accounts")
                    if not legal_expertise:
                        raise HTTPException(status_code=400, detail="Legal expertise areas are required for Lawyer accounts")
                
                elif professional_type == ProfessionalType.LAW_FIRM:
                    if not firm_name:
                        raise HTTPException(status_code=400, detail="Firm name is required for Law Firm accounts")
                    if not date_of_incorporation:
                        raise HTTPException(status_code=400, detail="Date of incorporation is required for Law Firm accounts")
                    if not company_registration_number:
                        raise HTTPException(status_code=400, detail="Company registration number is required for Law Firm accounts")
                    if not sra_bsb:
                        raise HTTPException(status_code=400, detail="Regulatory body (SRA/BSB/IAA) is required for Law Firm accounts")
                
                elif professional_type == ProfessionalType.HR_LEGAL:
                    if not hr_legal_type:
                        raise HTTPException(status_code=400, detail="HR/Legal type is required for HR/Legal accounts")
                    if not company_name:
                        raise HTTPException(status_code=400, detail="Company name is required for HR/Legal accounts")
                    if not company_registration_number:
                        raise HTTPException(status_code=400, detail="Company registration number is required for HR/Legal accounts")
                
                elif professional_type == ProfessionalType.A2J_PARTNERS:
                    if not organisation_name:
                        raise HTTPException(status_code=400, detail="Organisation name is required for A2J Partners accounts")
                    if not date_of_establishment:
                        raise HTTPException(status_code=400, detail="Date of establishment is required for A2J Partners accounts")
                    if not organisation_type:
                        raise HTTPException(status_code=400, detail="Organisation type is required for A2J Partners accounts")
                    if not areas_of_legal_focus:
                        raise HTTPException(status_code=400, detail="Areas of legal focus are required for A2J Partners accounts")
                    if not contact_person:
                        raise HTTPException(status_code=400, detail="Contact person is required for A2J Partners accounts")
                    if not registration_charity_number:
                        raise HTTPException(status_code=400, detail="Registration/Charity number is required for A2J Partners accounts")
                
                # Generate client ID
                global client_id_counter
                with client_id_lock:
                    client_id_counter += 1
                    client = str(client_id_counter)
                
                # Convert enums to strings
                sra_bsb_str = sra_bsb.value if sra_bsb else None
                hr_legal_type_str = hr_legal_type.value if hr_legal_type else None
                organisation_type_str = organisation_type.value if organisation_type else None
                professional_type_str = professional_type.value if professional_type else None
                
                # Handle practice_areas - convert string input to list of valid enum values
                practice_areas_str = None
                if practice_areas:
                    # Split by comma if it's a comma-separated string
                    areas_list = [area.strip() for area in practice_areas.split(',')]
                    # Map to valid enum values
                    valid_areas = []
                    for area in areas_list:
                        # Try to find matching enum value
                        for practice_area in PracticeArea:
                            if practice_area.value.lower() == area.lower():
                                valid_areas.append(practice_area.value)
                                break
                    practice_areas_str = valid_areas if valid_areas else None
                
                # Handle areas_of_legal_focus for A2J Partners - convert string input to list of valid enum values
                areas_of_legal_focus_str = None
                if areas_of_legal_focus:
                    # Split by comma if it's a comma-separated string
                    areas_list = [area.strip() for area in areas_of_legal_focus.split(',')]
                    # Map to valid enum values
                    valid_areas = []
                    for area in areas_list:
                        # Try to find matching enum value
                        for practice_area in PracticeArea:
                            if practice_area.value.lower() == area.lower():
                                valid_areas.append(practice_area.value)
                                break
                    areas_of_legal_focus_str = valid_areas if valid_areas else None
                
                # Handle legal_expertise for Lawyers - convert string input to list of valid enum values
                legal_expertise_str = None
                if legal_expertise:
                    # Split by comma if it's a comma-separated string
                    areas_list = [area.strip() for area in legal_expertise.split(',')]
                    # Map to valid enum values
                    valid_areas = []
                    for area in areas_list:
                        # Try to find matching enum value
                        for practice_area in PracticeArea:
                            if practice_area.value.lower() == area.lower():
                                valid_areas.append(practice_area.value)
                                break
                    legal_expertise_str = valid_areas if valid_areas else None
                
                # Prepare professional registration data for email verification
                registration_data = {
                    "user_name": user_name,
                    "email": email,
                    "password": password,
                    "client": client,
                    "professional_type": professional_type_str,
                    "first_name": first_name,
                    "last_name": last_name,
                    "date_of_birth": date_of_birth.isoformat() if date_of_birth else None,
                    "address": address,
                    "town_city": town_city,
                    "country": country,
                    "postcode": postcode,
                    # Professional fields (available for all types)
                    "sra_bsb_iaa": sra_bsb_iaa,
                    "sra_bsb": sra_bsb_str,
                    # Lawyer specific fields
                    "professional_employer": professional_employer,
                    "legal_expertise": legal_expertise_str,
                    # Law Firm specific fields
                    "firm_name": firm_name,
                    "date_of_incorporation": date_of_incorporation.isoformat() if date_of_incorporation else None,
                    "practice_areas": practice_areas_str,
                    "contact_person": contact_person,
                    "company_registration_number": company_registration_number,
                    # HR/Legal (SME) specific fields
                    "hr_legal_type": hr_legal_type_str,
                    "sector": sector,
                    "company_name": company_name,
                    # A2J Partners specific fields
                    "organisation_name": organisation_name,
                    "date_of_establishment": date_of_establishment.isoformat() if date_of_establishment else None,
                    "organisation_type": organisation_type_str,
                    "areas_of_legal_focus": areas_of_legal_focus_str,
                    "registration_charity_number": registration_charity_number
                }
                
                # Create email verification token and send OTP
                from services.email import EmailService
                otp_data = self.auth.create_email_verification_token(email, registration_data)
                
                # Send verification email in background
                background_tasks.add_task(
                    EmailService.send_email_verification_email,
                    email,
                    otp_data["otp"]
                )
                
                return {
                    "message": "Professional registration initiated. Please check your email for verification code.",
                    "email": email,
                    "expires_in": "15 minutes",
                    "professional_type": professional_type_str,
                    "next_step": "verify_email"
                }
            
            except HTTPException as he:
                # Re-raise HTTP exceptions
                raise he
            except Exception as e:
                print(e)
                traceback.print_exc()
                raise HTTPException(status_code=500, detail="An unexpected error occurred")
        
        ############################################
        # Auth User                                #
        ############################################
        @router.post("/auth_user")
        async def login(request: Request, user_data: UserLogin):
            try:
                # Get both token and user details using auth_user
                token, user_details = self.auth.auth_user(
                    email=user_data.email,
                    password=user_data.password
                ) 
                
                # Extract user type from user details for frontend routing
                user_type = (user_details.get("professional_type") or "User") if user_details else "User"
                
                return {
                    "token": token,
                    "user": user_details,
                    "user_type": user_type
                }
                
            except HTTPException as he:
                raise he
            except Exception as e:
                logger.error(f"Login error: {str(e)}")
                raise HTTPException(status_code=500, detail="Login failed")
        
        @router.get("/verify_token")
        async def verify_token(request: Request):
            try:
                token_data = self.auth.verify_request_token(request)
                return {"valid": True, "user_data": token_data}
            except HTTPException as he:
                raise he
            except Exception as e:
                logger.error(f"Token verification error: {str(e)}")
                raise HTTPException(status_code=401, detail="Invalid token")
        
        @router.post("/logout")
        async def logout(request: Request):
            """
            Logout endpoint.
            Frontend should:
            1. Call this endpoint before clearing token
            2. Clear token from storage (localStorage/AsyncStorage)
            3. Clear any other user-specific data
            4. Redirect to login page
            """
            try:
                # Verify token and get user data
                token_data = self.auth.verify_request_token(request)
                user_id = token_data.get("user_id")
                
                if not user_id:
                    raise HTTPException(status_code=401, detail="Invalid token")
                
                return {
                    "success": True,
                    "message": "Logged out successfully"
                }
                
            except HTTPException as he:
                # Even if token is invalid, return success for logout
                # This ensures frontend can clean up properly
                logger.warning(f"Logout with invalid token: {he.detail}")
                return {
                    "success": True,
                    "message": "Logged out successfully"
                }
            except Exception as e:
                logger.error(f"Logout error: {str(e)}")
                # Still return success to allow frontend cleanup
                return {
                    "success": True,
                    "message": "Logged out successfully"
                }
        
        #############################################
        # Profile Management                       #
        #############################################
        @router.get("/profile")
        async def get_profile(request: Request):
            try:
                # Verify token and get user data
                token_data = self.auth.verify_request_token(request)
                user_id = token_data.get("user_id")
                
                if not user_id:
                    raise HTTPException(status_code=401, detail="Invalid token")
                
                # Get user profile from database
                profile = self.auth.get_user_profile(user_id)
                if profile.get("professional_type") == "Lawyer":
                    with self.db.get_session() as session:
                        profile["response_stats"] = get_lawyer_response_stats(session, user_id)
                
                # Prepare response
                response_data = {
                    "profile": profile,
                    "user_type": profile.get("professional_type", "User")
                }
                
                return response_data
            
            except HTTPException as he:
                # Re-raise HTTP exceptions with their original status code
                raise he
            except Exception as e:
                logger.error(f"Error getting profile: {str(e)}")
                raise HTTPException(status_code=500, detail="Failed to get profile")

        @router.put("/profile")
        async def update_profile(request: Request, profile_update: UnifiedProfileUpdate):
            try:
                # Verify token and get user data
                token_data = self.auth.verify_request_token(request)
                user_id = token_data.get("user_id")
                
                if not user_id:
                    raise HTTPException(status_code=401, detail="Invalid token")
                
                # Get current profile to determine user type
                current_profile = self.auth.get_user_profile(user_id)
                user_type = current_profile.get("professional_type")
                
                # Convert Pydantic model to dict, excluding unset values
                profile_data = profile_update.dict(exclude_unset=True)
                
                # Handle enum conversions for all user types
                if 'sra_bsb' in profile_data and profile_data['sra_bsb'] is not None:
                    profile_data['sra_bsb'] = profile_data['sra_bsb'].value if hasattr(profile_data['sra_bsb'], 'value') else profile_data['sra_bsb']
                
                if user_type == "HR/Legal (SME)" and 'hr_legal_type' in profile_data and profile_data['hr_legal_type'] is not None:
                    profile_data['hr_legal_type'] = profile_data['hr_legal_type'].value if hasattr(profile_data['hr_legal_type'], 'value') else profile_data['hr_legal_type']
                
                if user_type == "A2J Partners" and 'organisation_type' in profile_data and profile_data['organisation_type'] is not None:
                    profile_data['organisation_type'] = profile_data['organisation_type'].value if hasattr(profile_data['organisation_type'], 'value') else profile_data['organisation_type']
                
                # Handle date_of_establishment for A2J Partners
                if user_type == "A2J Partners" and 'date_of_establishment' in profile_data and profile_data['date_of_establishment']:
                    try:
                        # Parse date string in format YYYY-MM-DD
                        date_parts = profile_data['date_of_establishment'].split('-')
                        if len(date_parts) == 3:
                            year, month, day = map(int, date_parts)
                            profile_data['date_of_establishment'] = datetime(year, month, day).date()
                    except (ValueError, AttributeError):
                        raise HTTPException(status_code=400, detail="Invalid date format for date_of_establishment. Use YYYY-MM-DD format.")

                if 'date_of_birth' in profile_data and profile_data['date_of_birth']:
                    try:
                        profile_data['date_of_birth'] = datetime.fromisoformat(
                            profile_data['date_of_birth']
                        ).date()
                    except (ValueError, TypeError):
                        raise HTTPException(
                            status_code=400,
                            detail="Invalid date format for date_of_birth. Use YYYY-MM-DD format.",
                        )
                    if profile_data['date_of_birth'] >= datetime.now(timezone.utc).date():
                        raise HTTPException(status_code=400, detail="Date of birth must be in the past")
                elif profile_data.get('date_of_birth') == "":
                    profile_data.pop('date_of_birth')
                
                # Update user profile
                updated_profile = self.auth.update_user_profile(user_id, profile_data)
                
                # Prepare response
                response_data = {
                    "profile": updated_profile,
                    "user_type": user_type
                }
                
                return response_data
            
            except HTTPException as he:
                # Re-raise HTTP exceptions with their original status code
                raise he
            except Exception as e:
                logger.error(f"Error updating profile: {str(e)}")
                raise HTTPException(status_code=500, detail="Failed to update profile")
        
        #############################################
        # Profile Picture Management               #
        #############################################
        @router.post("/profile/picture")
        async def upload_profile_picture(request: Request, file: UploadFile = File(...)):
            try:
                # Verify token and get user data
                token_data = self.auth.verify_request_token(request)
                user_id = token_data.get("user_id")
                
                if not user_id:
                    raise HTTPException(status_code=401, detail="Invalid token")
                
                # Check file type
                allowed_extensions = [".jpg", ".jpeg", ".png", ".gif"]
                file_extension = os.path.splitext(file.filename)[1].lower()
                
                if file_extension not in allowed_extensions:
                    raise HTTPException(
                        status_code=400, 
                        detail=f"File type not allowed. Allowed types: {', '.join(allowed_extensions)}"
                    )
                
                # Check file size (limit to 5MB)
                max_size = 5 * 1024 * 1024  # 5MB
                file_size = 0
                
                # Read and count file size
                file_content = await file.read()
                file_size = len(file_content)
                
                # Reset file position
                await file.seek(0)
                
                if file_size > max_size:
                    raise HTTPException(
                        status_code=400,
                        detail=f"File size exceeds the limit of 5MB"
                    )
                
                # Upload profile picture to S3
                result = self.auth.upload_profile_picture(user_id, file)
                
                return {"message": "Profile picture uploaded successfully", "file_info": result}
            
            except HTTPException as he:
                raise he
            except Exception as e:
                logger.error(f"Error uploading profile picture: {str(e)}")
                raise HTTPException(status_code=500, detail="Failed to upload profile picture")

        @router.get("/profile/picture")
        async def get_profile_picture(request: Request):
            try:
                # Verify token and get user data
                token_data = self.auth.verify_request_token(request)
                user_id = token_data.get("user_id")
                
                if not user_id:
                    raise HTTPException(status_code=401, detail="Invalid token")
                
                # Get profile picture URL
                picture_url = self.auth.get_profile_picture_url(user_id)
                
                if not picture_url:
                    raise HTTPException(status_code=404, detail="No profile picture found")
                
                # Prepare response data
                response_data = {"url": picture_url}
                
                return response_data
            
            except HTTPException as he:
                raise he
            except Exception as e:
                logger.error(f"Error getting profile picture: {str(e)}")
                raise HTTPException(status_code=500, detail="Failed to get profile picture")

        @router.delete("/profile/picture")
        async def delete_profile_picture(request: Request):
            try:
                # Verify token and get user data
                token_data = self.auth.verify_request_token(request)
                user_id = token_data.get("user_id")
                
                if not user_id:
                    raise HTTPException(status_code=401, detail="Invalid token")
                
                # Delete profile picture
                result = self.auth.delete_profile_picture(user_id)
                
                return result
            
            except HTTPException as he:
                raise he
            except Exception as e:
                logger.error(f"Error deleting profile picture: {str(e)}")
                raise HTTPException(status_code=500, detail="Failed to delete profile picture")

        #############################################
        # Work Experience Management               #
        #############################################
        @router.post("/profile/work-experience")
        async def add_work_experience(request: Request, work_experience: WorkExperienceCreate):
            try:
                # Verify token and get user data
                token_data = self.auth.verify_request_token(request)
                user_id = token_data.get("user_id")
                
                if not user_id:
                    raise HTTPException(status_code=401, detail="Invalid token")
                
                # Add work experience
                work_experience_data = work_experience.dict()
                result = self.auth.add_work_experience(user_id, work_experience_data)
                
                return {"work_experience": result}
            
            except HTTPException as he:
                raise he
            except Exception as e:
                logger.error(f"Error adding work experience: {str(e)}")
                raise HTTPException(status_code=500, detail="Failed to add work experience")

        @router.get("/profile/work-experience")
        async def get_work_experiences(request: Request):
            try:
                # Verify token and get user data
                token_data = self.auth.verify_request_token(request)
                user_id = token_data.get("user_id")
                
                if not user_id:
                    raise HTTPException(status_code=401, detail="Invalid token")
                
                # Get work experiences
                work_experiences = self.auth.get_work_experiences(user_id)
                return {"work_experiences": work_experiences}
            
            except HTTPException as he:
                raise he
            except Exception as e:
                logger.error(f"Error getting work experiences: {str(e)}")
                raise HTTPException(status_code=500, detail="Failed to get work experiences")

        @router.put("/profile/work-experience/{work_experience_id}")
        async def update_work_experience(request: Request, work_experience_id: str, work_experience: WorkExperienceUpdate):
            try:
                # Verify token and get user data
                token_data = self.auth.verify_request_token(request)
                user_id = token_data.get("user_id")
                
                if not user_id:
                    raise HTTPException(status_code=401, detail="Invalid token")
                
                # Update work experience
                work_experience_data = work_experience.dict(exclude_unset=True)
                result = self.auth.update_work_experience(user_id, work_experience_id, work_experience_data)
                
                return {"work_experience": result}
            
            except HTTPException as he:
                raise he
            except Exception as e:
                logger.error(f"Error updating work experience: {str(e)}")
                raise HTTPException(status_code=500, detail="Failed to update work experience")

        @router.delete("/profile/work-experience/{work_experience_id}")
        async def delete_work_experience(request: Request, work_experience_id: str):
            try:
                # Verify token and get user data
                token_data = self.auth.verify_request_token(request)
                user_id = token_data.get("user_id")
                
                if not user_id:
                    raise HTTPException(status_code=401, detail="Invalid token")
                
                # Delete work experience
                result = self.auth.delete_work_experience(user_id, work_experience_id)
                
                return result
            
            except HTTPException as he:
                raise he
            except Exception as e:
                logger.error(f"Error deleting work experience: {str(e)}")
                raise HTTPException(status_code=500, detail="Failed to delete work experience")

        #############################################
        # Legal Services and Skills                #
        #############################################
        @router.get("/legal-services")
        async def get_legal_services():
            try:
                return {"legal_services": get_service_catalog()}
            except Exception as e:
                logger.error(f"Error getting legal services: {str(e)}")
                raise HTTPException(status_code=500, detail="Failed to get legal services")

        @router.get("/legal-skills")
        async def get_legal_skills():
            try:
                return {"legal_skills": get_skill_catalog()}
            except Exception as e:
                logger.error(f"Error getting legal skills: {str(e)}")
                raise HTTPException(status_code=500, detail="Failed to get legal skills")

        @router.get("/practice-areas")
        async def get_practice_areas():
            try:
                return {"practice_areas": get_practice_area_catalog()}
            except Exception as e:
                logger.error(f"Error getting practice areas: {str(e)}")
                raise HTTPException(status_code=500, detail="Failed to get practice areas")

        #############################################
        # Booking Availability & Presence           #
        #############################################
        @router.get("/professionals/me/availability")
        async def get_my_availability(request: Request):
            try:
                token_data = self.auth.verify_request_token(request)
                user_id = token_data.get("user_id")
                if not user_id:
                    raise HTTPException(status_code=401, detail="Invalid token")
                with self.db.get_session() as session:
                    professional = self._require_professional(session, user_id)
                    return BookingService(session).get_availability(professional.id)
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error getting availability: {str(e)}")
                raise HTTPException(status_code=500, detail="Failed to get availability")

        @router.put("/professionals/me/availability")
        async def put_my_availability(request: Request, payload: AvailabilityPayload):
            try:
                token_data = self.auth.verify_request_token(request)
                user_id = token_data.get("user_id")
                if not user_id:
                    raise HTTPException(status_code=401, detail="Invalid token")
                with self.db.get_session() as session:
                    professional = self._require_professional(session, user_id)
                    try:
                        availability = BookingService(session).replace_availability(
                            professional.id,
                            rules=[item.model_dump() for item in payload.rules],
                            exceptions=[item.model_dump() for item in payload.exceptions],
                        )
                    except ValueError as exc:
                        raise HTTPException(status_code=400, detail=str(exc))
                    return {"success": True, "availability": availability}
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error updating availability: {str(e)}")
                raise HTTPException(status_code=500, detail="Failed to update availability")

        @router.get("/professionals/{professional_id}/booking-slots")
        async def get_booking_slots(
            professional_id: str,
            request: Request,
            from_at: str = Query(..., alias="from", description="ISO-8601 start datetime"),
            to: str = Query(..., description="ISO-8601 end datetime"),
            consultation_type: str = Query("single_30_min"),
        ):
            try:
                token_data = self.auth.verify_request_token(request)
                if not token_data.get("user_id"):
                    raise HTTPException(status_code=401, detail="Invalid token")
                try:
                    professional_uuid = uuid.UUID(professional_id)
                    start_at = parse_datetime(from_at, field_name="from")
                    end_at = parse_datetime(to, field_name="to")
                except ValueError as exc:
                    raise HTTPException(status_code=400, detail=str(exc))

                with self.db.get_session() as session:
                    professional = session.query(User).filter(
                        User.id == professional_uuid,
                        User.professional_type.in_(["Lawyer", "Law Firm", "A2J Partners"]),
                    ).first()
                    if not professional:
                        raise HTTPException(status_code=404, detail="Professional not found")
                    try:
                        slots = BookingService(session).generate_slots(
                            professional_uuid,
                            start_at=start_at,
                            end_at=end_at,
                            consultation_type=consultation_type,
                        )
                    except ValueError as exc:
                        raise HTTPException(status_code=400, detail=str(exc))
                    return {
                        "professional_id": professional_id,
                        "consultation_type": consultation_type,
                        "duration_minutes": consultation_duration_minutes(consultation_type),
                        "slots": [slot.as_dict() for slot in slots],
                    }
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error getting booking slots: {str(e)}")
                raise HTTPException(status_code=500, detail="Failed to get booking slots")

        @router.post("/professionals/me/presence")
        async def update_my_presence(request: Request, payload: PresencePayload):
            try:
                token_data = self.auth.verify_request_token(request)
                user_id = token_data.get("user_id")
                if not user_id:
                    raise HTTPException(status_code=401, detail="Invalid token")
                with self.db.get_session() as session:
                    professional = self._require_professional(session, user_id)
                    try:
                        presence, status, label = BookingService(session).upsert_presence(
                            professional.id,
                            manual_status_override=payload.manual_status_override,
                            accepting_consultations=payload.accepting_consultations,
                            heartbeat=payload.heartbeat,
                        )
                    except ValueError as exc:
                        raise HTTPException(status_code=400, detail=str(exc))
                    return {
                        "success": True,
                        "availability_status": status,
                        "availability_label": label,
                        "last_heartbeat_at": ensure_utc(presence.last_heartbeat_at).isoformat() if presence.last_heartbeat_at else None,
                        "manual_status_override": presence.manual_status_override,
                        "accepting_consultations": presence.accepting_consultations,
                    }
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error updating presence: {str(e)}")
                raise HTTPException(status_code=500, detail="Failed to update presence")

        #############################################
        # Enhanced Professional Search             #
        #############################################
        @router.get("/search/professionals")
        async def search_professionals(
            request: Request,
            # Search filters
            q: Optional[str] = Query(None, description="Search query for name, expertise, or services"),
            practice_area: Optional[str] = Query(None, description="Filter by legal practice area"),
            professional_type: Optional[str] = Query(None, description="Filter by professional type: Lawyer, Law Firm, A2J Partners, HR/Legal (SME)"),
            location: Optional[str] = Query(None, description="Filter by location/city"),
            services: Optional[str] = Query(None, description="Filter by specific services (comma-separated)"),
            
            # Pagination
            page: int = Query(1, ge=1, description="Page number"),
            limit: int = Query(20, ge=1, le=50, description="Results per page"),
            
            # Sorting
            sort_by: Optional[str] = Query("random", description="Sort by: random, alphabetical, relevance, experience"),
            random_seed: Optional[str] = Query(None, max_length=64, description="Seed used to keep random pagination stable")
        ):
            """
            Enhanced search for legal professionals with filtering, pagination, and sorting.
            By default, displays all professionals in a deterministic random order.
            The random seed keeps the order stable while the client paginates.
            Returns professional profiles optimized for search display cards.
            """
            try:
                # Verify token and get user data
                token_data = self.auth.verify_request_token(request)
                user_id = token_data.get("user_id")
                
                if not user_id:
                    raise HTTPException(status_code=401, detail="Invalid token")
                
                # Verify the requesting user is a normal user (not a pro user)
                user_profile = self.auth.get_user_profile(user_id)
                user_type = user_profile.get("professional_type")
                
                # Only allow normal users to access this endpoint
                if user_type and user_type not in [None, "", "None"]:
                    raise HTTPException(
                        status_code=403, 
                        detail="This endpoint is only available to normal users"
                    )
                
                # Build query with filters
                with self.db.get_session() as session:
                    # Base query for all professional users
                    query = session.query(User).filter(
                        User.professional_type.in_([
                            "Lawyer",
                            "Law Firm",
                            "A2J Partners"
                        ]),
                        User.is_search_visible.is_(True),
                        latest_persona_verification_approved_clause(User.id),
                    )
                    
                    # Apply search filters
                    if q:
                        search_term = f"%{q.lower()}%"
                        search_filter = or_(
                            func.lower(User.first_name).like(search_term),
                            func.lower(User.last_name).like(search_term),
                            func.lower(User.firm_name).like(search_term),
                            func.lower(User.organisation_name).like(search_term),
                            func.lower(User.company_name).like(search_term),
                            func.lower(User.about).like(search_term),
                            func.lower(User.tagline).like(search_term)
                        )
                        query = query.filter(search_filter)
                    
                    if professional_type:
                        query = query.filter(User.professional_type == professional_type)
                    
                    if practice_area:
                        pa_labels = resolve_practice_filter(practice_area)
                        if pa_labels:
                            pa_filters = []
                            for lab in pa_labels:
                                pa_filters.append(or_(
                                    User.legal_expertise.contains([lab]),
                                    User.practice_areas.contains([lab]),
                                    User.areas_of_legal_focus.contains([lab]),
                                ))
                            query = query.filter(or_(*pa_filters))
                    
                    if location:
                        location_term = f"%{location.lower()}%"
                        location_filter = or_(
                            func.lower(User.town_city).like(location_term),
                            func.lower(User.country).like(location_term),
                            func.lower(User.firm_address).like(location_term)
                        )
                        query = query.filter(location_filter)
                    
                    if services:
                        svc_labels = resolve_service_filters([s.strip() for s in services.split(',')])
                        svc_filters = [User.services.contains([lab]) for lab in svc_labels]
                        if svc_filters:
                            query = query.filter(or_(*svc_filters))
                    
                    # Apply sorting
                    if sort_by == "random":
                        # Use a client-provided seed so page 2 follows page 1 in
                        # the same order while a new search session can reshuffle.
                        seed = random_seed or datetime.now(timezone.utc).strftime("%Y-%m-%d")
                        random_order = func.md5(
                            func.concat(cast(User.id, String), literal(seed))
                        )
                        query = query.order_by(random_order.asc(), User.id.asc())
                    elif sort_by == "alphabetical" or not q:
                        # Sort by name (first_name for individuals, firm_name/organisation_name/company_name for entities)
                        query = query.order_by(
                            func.coalesce(
                                User.firm_name,
                                User.organisation_name, 
                                User.company_name,
                                func.concat(User.first_name, ' ', User.last_name)
                            ).asc()
                        )
                    elif sort_by == "experience":
                        # Sort by creation date (older accounts first, assuming more experience)
                        query = query.order_by(User.created_at.asc())
                    # Note: relevance sorting would be applied after getting results if search query exists
                    
                    # Get total count before pagination
                    total_count = query.count()
                    
                    # Apply pagination
                    offset = (page - 1) * limit
                    professionals = query.offset(offset).limit(limit).all()
                    
                    # Transform to simplified professional format
                    results = []
                    for prof in professionals:
                        try:
                            expertise = search_expertise_payload(prof)
                            svc_ids, svc_items = labels_to_service_items(prof.services)
                            simplified_profile = {
                                'id': str(prof.id),
                                'display_name': self._get_display_name(prof),
                                'firm_name': prof.firm_name,
                                'town_city': prof.town_city,
                                'profile_picture_path': prof.profile_picture_path,
                                'legal_expertise_ids': expertise['legal_expertise_ids'],
                                'legal_expertise': expertise['legal_expertise'],
                                'service_ids': svc_ids,
                                'services': svc_items,
                                'sra_bsb': prof.sra_bsb,
                                'tagline': prof.tagline,
                                'hourly_rate': float(prof.hourly_rate) if prof.hourly_rate else None,
                                'firm_hourly_rate': float(prof.firm_hourly_rate) if prof.firm_hourly_rate else None,
                                'push_token': self._get_user_push_token(session, str(prof.id))
                            }
                            simplified_profile.update(self._availability_summary(session, prof.id))
                            if prof.professional_type == "Lawyer":
                                simplified_profile["response_stats"] = get_lawyer_response_stats(
                                    session, prof.id
                                )
                            
                            # Add review stats for search cards
                            try:
                                review_stats = self.review_service.get_lawyer_stats(lawyer_id=str(prof.id))
                                simplified_profile['review_stats'] = review_stats
                            except Exception as e:
                                logger.debug(f"Could not fetch review stats for {prof.id} in search: {str(e)}")
                                simplified_profile['review_stats'] = {"total_reviews": 0, "average_rating": 0.0}
                            
                            # Add match score if search query exists
                            if q:
                                simplified_profile['match_score'] = self._calculate_search_relevance(prof, q, practice_area)
                            
                            results.append(simplified_profile)
                            
                        except Exception as e:
                            logger.warning(f"Error processing profile for user {prof.id}: {str(e)}")
                            continue
                    
                    # Sort by relevance if search query exists and relevance sorting is requested
                    if q and sort_by == "relevance":
                        results.sort(key=lambda x: x.get('match_score', 0), reverse=True)
                    
                    # Calculate pagination metadata
                    total_pages = (total_count + limit - 1) // limit
                    has_next = page < total_pages
                    has_prev = page > 1
                
                # Prepare response
                return {
                    "professionals": results,
                    "pagination": {
                        "page": page,
                        "limit": limit,
                        "total": total_count,
                        "total_pages": total_pages,
                        "has_next": has_next,
                        "has_prev": has_prev
                    },
                    "filters_applied": {
                        "search_query": q,
                        "practice_area": practice_area,
                        "professional_type": professional_type,
                        "location": location,
                        "services": services,
                        "sort_by": sort_by,
                        "random_seed": random_seed if sort_by == "random" else None,
                    },
                    "available_filters": {
                        "professional_types": ["Lawyer", "Law Firm", "A2J Partners"],
                        "practice_areas": self._get_available_practice_areas(),
                        "common_services": self._get_common_services()
                    }
                }
            
            except HTTPException as he:
                # Re-raise HTTP exceptions with their original status code
                raise he
            except Exception as e:
                logger.error(f"Error in professional search: {str(e)}")
                raise HTTPException(status_code=500, detail="Search failed")

        #############################################
        # Professional Profile Details             #
        #############################################
        @router.get("/professionals/{professional_id}/profile")
        async def get_professional_profile(
            professional_id: str,
            request: Request
        ):
            """
            Get detailed profile of a specific professional.
            This endpoint is called when a user clicks on a professional's card from search results.
            
            Args:
                professional_id: UUID of the professional
                request: FastAPI request object for authentication
                
            Returns:
                Complete professional profile with all details
            """
            try:
                # Verify authentication
                token_data = self.auth.verify_request_token(request)
                user_id = token_data.get("user_id")
                
                if not user_id:
                    raise HTTPException(status_code=401, detail="Invalid token")
                
                user_profile = self.auth.get_user_profile(user_id)
                
                # Only allow normal users to access professional profiles
                if user_profile.get("professional_type") in ["Lawyer", "Law Firm"]:
                    raise HTTPException(
                        status_code=403, 
                        detail="This endpoint is only available to normal users"
                    )
                
                # Validate professional_id format
                try:
                    professional_uuid = uuid.UUID(professional_id)
                except ValueError:
                    raise HTTPException(status_code=400, detail="Invalid professional ID format")
                
                # Get professional from database
                with self.db.get_session() as session:
                    professional = session.query(User).filter(
                        User.id == professional_uuid,
                        User.professional_type.in_(["Lawyer", "Law Firm", "A2J Partners"]),
                        User.is_search_visible.is_(True),
                        latest_persona_verification_approved_clause(User.id),
                    ).first()
                    
                    if not professional:
                        raise HTTPException(status_code=404, detail="Professional not found")
                    
                    # Get complete profile data
                    full_profile = self.auth.get_user_profile(professional_id)
                    
                    if not full_profile:
                        raise HTTPException(status_code=404, detail="Professional profile not found")
                    
                    # Return specific profile fields only
                    profile = {
                        "id": full_profile.get("id"),
                        "first_name": full_profile.get("first_name"),
                        "last_name": full_profile.get("last_name"),
                        "firm_name": full_profile.get("firm_name"),
                        "town_city": full_profile.get("town_city"),
                        "profile_picture_path": full_profile.get("profile_picture_path"),
                        "legal_expertise": full_profile.get("legal_expertise"),
                        "skills": full_profile.get("skills"),
                        "sra_bsb": full_profile.get("sra_bsb"),
                        "sra_bsb_iaa": full_profile.get("sra_bsb_iaa"),
                        "tagline": full_profile.get("tagline"),
                        "about": full_profile.get("about"),
                        "work_experiences": full_profile.get("work_experiences"),
                        "member_since": professional.created_at.isoformat() if professional.created_at else None,
                        "hourly_rate": float(full_profile.get("hourly_rate")) if full_profile.get("hourly_rate") else None,
                        "firm_hourly_rate": float(full_profile.get("firm_hourly_rate")) if full_profile.get("firm_hourly_rate") else None,
                        "push_token": self._get_user_push_token(session, professional_id)
                    }
                    profile.update(self._availability_summary(session, professional.id))
                    if professional.professional_type == "Lawyer":
                        profile["response_stats"] = get_lawyer_response_stats(
                            session, professional.id
                        )
                    
                    # Get review statistics
                    try:
                        review_stats = self.review_service.get_lawyer_stats(lawyer_id=professional_id)
                        profile["review_stats"] = review_stats
                    except Exception as e:
                        logger.warning(f"Could not fetch review stats for {professional_id}: {str(e)}")
                        profile["review_stats"] = {"total_reviews": 0, "average_rating": 0.0}
                    
                    # Get recent reviews (first 5)
                    try:
                        recent_reviews = self.review_service.get_lawyer_reviews(
                            lawyer_id=professional_id, 
                            page=1, 
                            limit=5
                        )
                        profile["recent_reviews"] = recent_reviews
                    except Exception as e:
                        logger.warning(f"Could not fetch recent reviews for {professional_id}: {str(e)}")
                        profile["recent_reviews"] = []
                    
                    # Prepare response
                    response_data = {
                        "professional": profile,
                        "success": True,
                        "message": "Professional profile retrieved successfully"
                    }
                    
                    return response_data
                    
            except HTTPException as he:
                # Re-raise HTTP exceptions with their original status code
                raise he
            except Exception as e:
                logger.error(f"Error retrieving professional profile {professional_id}: {str(e)}")
                raise HTTPException(status_code=500, detail="Failed to retrieve professional profile")

        #############################################
        # Account Deletion                          #
        #############################################
        @router.delete("/account")
        async def delete_account(request: Request, background_tasks: BackgroundTasks):
            """
            Delete the authenticated user's account.
            This will delete all user data and associated files.
            """
            try:
                # Verify token and get user data
                token_data = self.auth.verify_request_token(request)
                user_id = token_data.get("user_id")
                
                if not user_id:
                    raise HTTPException(status_code=401, detail="Invalid token")
                
                # Convert string user_id to UUID if needed
                try:
                    if isinstance(user_id, str):
                        user_id_uuid = uuid.UUID(user_id)
                    else:
                        user_id_uuid = user_id
                except ValueError:
                    raise HTTPException(status_code=400, detail="Invalid user ID in token")
                
                # Fetch user details BEFORE deletion, so we have email/first_name
                # for the confirmation email after the account is gone
                user_record = self.auth.get_user_profile(user_id_uuid)
                user_email = (user_record or {}).get("email")
                user_first_name = (user_record or {}).get("first_name")
                
                # Delete the user account
                result = self.auth.delete_user_account(user_id_uuid)
                
                # Send deletion confirmation email (fire-and-forget, after success)
                if user_email:
                    from services.email import EmailService
                    background_tasks.add_task(
                        EmailService.send_account_deletion_email,
                        user_email,
                        user_first_name,
                    )
                
                return result
                
            except HTTPException as he:
                raise he
            except Exception as e:
                logger.error(f"Error deleting account: {str(e)}")
                raise HTTPException(status_code=500, detail="Failed to delete account") 
        
        
        @router.delete("/backend/users/{user_id}")
        async def delete_user_backend(
            user_id: str,
            x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token"),
        ):
            """
            Backend endpoint to delete a user by ID.
            This endpoint is for backend/admin use only and requires X-Admin-Token.
            Use with caution - this permanently deletes user data.
            """
            try:
                AdminAPI._require_admin_token(x_admin_token)

                # Delete the user account
                result = self.auth.delete_user_by_id_backend(user_id)
                return result
                
            except HTTPException as he:
                raise he
            except Exception as e:
                logger.error(f"Error deleting user via backend: {str(e)}")
                raise HTTPException(status_code=500, detail="Failed to delete user")
        
        #############################################
        # Request Service Endpoint                 #
        #############################################
        @router.post("/search/request-service/{preferred_lawyer_id}")
        async def request_service(
            request: Request,
            preferred_lawyer_id: str,
            service: str = Form(..., description="Specific service: road traffic offences, employment law, family law, or immigration"),
            consultation_category: str = Form(..., description="Single Consultation, Ongoing Support, Casework, or Not Sure"),
            consultation_type: Optional[str] = Form(None, description="Specific consultation type"),
            budget_range: Optional[str] = Form(
                None,
                description="Budget range. Optional for casework; derived from casework_budget_gbp.",
            ),
            casework_service_type: Optional[str] = Form(
                None,
                description=(
                    "Required when consultation_category=casework. One of the 13 "
                    "casework services, e.g. skilled_worker_visa, student_visa, "
                    "visit_visa, ancestry_visa, spouse_partner_visa, pbs_dependants, "
                    "family_reunion, ilr_10_year, naturalisation, eu_settlement_scheme, "
                    "flr_fp_human_rights, asylum_full_support, full_case_management."
                ),
            ),
            casework_budget_gbp: Optional[int] = Form(
                None,
                description=(
                    "Exact £ budget for casework. Must be >= the published minimum "
                    "for the chosen casework_service_type (e.g. >= 1000 for student_visa)."
                ),
            ),
            urgency_level: str = Form(..., description="Urgency: urgent, within_week, within_month, not_urgent"),
            user_location: Optional[str] = Form(None, description="User's location"),
            booking_start_at: Optional[str] = Form(
                None,
                description="Required for single_consultation and ongoing_support. ISO-8601 UTC or offset datetime.",
            ),
            booking_timezone: Optional[str] = Form(
                "Europe/London",
                description="Timezone selected/displayed by the user for the booking slot.",
            ),
            additional_notes: Optional[str] = Form(None, description="Additional notes or details"),
            file_types: Optional[str] = Form(
                None,
                description="Comma-separated type per file: pdf, docx, image, voice_note. "
                            "Position maps to the uploaded files. When omitted the server "
                            "infers from filename/content.",
            ),
            attachment: Optional[UploadFile] = File(
                None, description="Single file upload (use 'attachments' for multiple)"
            ),
            attachments: Optional[UploadFile] = File(
                None, description="File upload(s) — repeat this field for multiple files"
            ),
        ):
            """
            Request service from the search tab. This endpoint mimics the referral flow
            but is standalone and allows users to directly request services from lawyers
            found in the search results.
            
            The request is sent directly to lawyers' inbox just like in the referral flow.
            Accepts optional PDF or voice recording attachments.
            
            Expected values to match referral flow:
            - consultation_category: "single_consultation", "ongoing_support", "casework", "not_sure"
            - consultation_type: "single_5_min", "single_10_min", "single_20_min", "single_30_min", "single_1_hour", "ongoing_3_day", "ongoing_5_day", "casework", "not_sure" (optional for casework/not_sure; required for single_consultation / ongoing_support)
            - budget_range: casework users may omit this; it is auto-derived from casework_budget_gbp.
              For non-casework, use "below_300", "300_to_499", "500_to_699", "above_700",
              or ongoing-specific values as documented.
            - casework_service_type: REQUIRED for casework. One of the 13 services (skilled_worker_visa,
              student_visa, visit_visa, ancestry_visa, spouse_partner_visa, pbs_dependants,
              family_reunion, ilr_10_year, naturalisation, eu_settlement_scheme,
              flr_fp_human_rights, asylum_full_support, full_case_management).
            - casework_budget_gbp: REQUIRED for casework. Must be >= the published minimum for the
              chosen service (e.g. >= 1000 for student_visa).
            - urgency_level: "urgent", "within_week", "within_month", "not_urgent"
            """
            try:
                # Validate consultation_category matches referral flow values
                valid_consultation_categories = ["single_consultation", "ongoing_support", "casework", "not_sure"]
                if consultation_category not in valid_consultation_categories:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Invalid consultation_category. Must be one of: {', '.join(valid_consultation_categories)}"
                    )

                # Derive consultation_type when omitted (referral_requests.consultation_type is NOT NULL)
                ct_normalized = (consultation_type or "").strip()
                if not ct_normalized:
                    if consultation_category == "casework":
                        consultation_type = ConsultationType.CASEWORK.value
                    elif consultation_category == "not_sure":
                        consultation_type = ConsultationType.NOT_SURE.value
                    else:
                        raise HTTPException(
                            status_code=400,
                            detail=(
                                "consultation_type is required for single_consultation and ongoing_support "
                                "(e.g. single_20_min, ongoing_3_day)."
                            ),
                        )
                else:
                    consultation_type = ct_normalized
                
                # Validate urgency_level matches referral flow values
                valid_urgency_levels = ["urgent", "within_week", "within_month", "not_urgent"]
                if urgency_level not in valid_urgency_levels:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Invalid urgency_level. Must be one of: {', '.join(valid_urgency_levels)}"
                    )

                # Casework: requires a Service & Price selection. The exact £
                # budget the user committed to must be >= the published minimum
                # for that service (e.g. >= £1,000 for student_visa). The
                # legacy budget_range parameter is optional; if supplied it may
                # be 'above_700' or the exact casework_budget_gbp value.
                if consultation_category == "casework":
                    if not casework_service_type or casework_service_type not in CASEWORK_MIN_BUDGET:
                        raise HTTPException(
                            status_code=400,
                            detail=(
                                "Casework requires casework_service_type. Must be one of: "
                                + ", ".join(CASEWORK_SERVICE_VALUES)
                            ),
                        )
                    if casework_budget_gbp is None:
                        raise HTTPException(
                            status_code=400,
                            detail=(
                                f"Casework requires casework_budget_gbp. Minimum for "
                                f"{CASEWORK_LABEL_BY_SERVICE[casework_service_type]} is "
                                f"£{CASEWORK_MIN_BUDGET[casework_service_type]:,} "
                                f"(estimated range: {CASEWORK_PRICE_BY_SERVICE[casework_service_type]})."
                            ),
                        )
                    floor_err = validate_casework_budget(casework_service_type, int(casework_budget_gbp))
                    if floor_err:
                        raise HTTPException(status_code=400, detail=floor_err)

                    # Backfill budget_range so downstream normalisation/display
                    # code keeps working unchanged.
                    if not (budget_range or "").strip():
                        budget_range = str(int(casework_budget_gbp))
                    else:
                        br_casework = budget_range.strip()
                        # Allow either the canonical "above_700" or an exact
                        # numeric string matching casework_budget_gbp.
                        if br_casework != "above_700":
                            try:
                                if int(br_casework) != int(casework_budget_gbp):
                                    raise HTTPException(
                                        status_code=400,
                                        detail=(
                                            "budget_range must match casework_budget_gbp or be 'above_700'."
                                        ),
                                    )
                            except ValueError:
                                raise HTTPException(
                                    status_code=400,
                                    detail=(
                                        "Casework only accepts numeric budget_range matching casework_budget_gbp, "
                                        "or the legacy bucket 'above_700'."
                                    ),
                                )
                
                validate_request_service_budget_range(
                    consultation_category,
                    consultation_type,
                    budget_range,
                )
                
                valid_consultation_types = [
                    "single_5_min",
                    "single_10_min",
                    "single_20_min",
                    "single_30_min",
                    "single_1_hour",
                    "ongoing_3_day",
                    "ongoing_5_day",
                    "casework",
                    ConsultationType.NOT_SURE.value,
                ]
                if consultation_type not in valid_consultation_types:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Invalid consultation_type. Must be one of: {', '.join(valid_consultation_types)}",
                    )
                
                # Verify authentication
                token_data = self.auth.verify_request_token(request)
                user_id = token_data.get("user_id")
                
                if not user_id:
                    raise HTTPException(status_code=401, detail="Invalid token")
                
                # Verify user is a normal user (not professional)
                user_profile = self.auth.get_user_profile(user_id)
                if user_profile.get("professional_type") in ["Lawyer", "Law Firm"]:
                    raise HTTPException(
                        status_code=403, 
                        detail="This endpoint is only available to normal users"
                    )
                
                # Validate required fields
                if not service or not consultation_category or not urgency_level:
                    raise HTTPException(
                        status_code=400, 
                        detail="Missing required fields: service, consultation_category, urgency_level"
                    )
                
                # Validate service value and derive legal area from service
                service_to_area_mapping = {
                    "immigration": "immigration",           # matches referral flow
                    "family": "family",                     # matches referral flow  
                    "employment": "employment",             # matches referral flow
                    "road_traffic": "road_traffic"          # matches referral flow
                }
                
                if service not in service_to_area_mapping:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Invalid service. Must be one of: {', '.join(service_to_area_mapping.keys())}"
                    )
                
                # Use service directly as legal area since they now match
                legal_area = service

                requested_start_at = None
                requested_end_at = None
                if requires_booking_slot(consultation_category):
                    if not booking_start_at:
                        raise HTTPException(
                            status_code=400,
                            detail="booking_start_at is required for consultation requests.",
                        )
                    try:
                        requested_start_at = parse_datetime(booking_start_at, field_name="booking_start_at")
                    except ValueError as exc:
                        raise HTTPException(status_code=400, detail=str(exc))
                    requested_end_at = requested_start_at + timedelta(
                        minutes=consultation_duration_minutes(consultation_type)
                    )
                    if requested_start_at <= datetime.now(timezone.utc):
                        raise HTTPException(status_code=400, detail="booking_start_at must be in the future")

                    with self.db.get_session() as session:
                        try:
                            lawyer_uuid = uuid.UUID(preferred_lawyer_id)
                        except ValueError:
                            raise HTTPException(status_code=400, detail="Invalid lawyer ID format")
                        lawyer = session.query(User).filter(
                            User.id == lawyer_uuid,
                            User.professional_type == 'Lawyer'
                        ).first()
                        if not lawyer:
                            raise HTTPException(status_code=400, detail="Selected lawyer not found")
                        if not BookingService(session).validate_slot(
                            lawyer_uuid,
                            requested_start_at=requested_start_at,
                            requested_end_at=requested_end_at,
                            consultation_type=consultation_type,
                        ):
                            raise HTTPException(
                                status_code=409,
                                detail="Selected booking slot is no longer available.",
                            )
                
                allowed_content_types = [
                    'application/pdf',
                    'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                    'application/msword',
                    'image/jpeg', 'image/jpg', 'image/png', 'image/gif',
                    'image/bmp', 'image/webp', 'image/tiff', 'image/svg+xml',
                    'audio/mpeg', 'audio/mp3', 'audio/wav', 'audio/m4a', 'audio/ogg',
                    'audio/webm', 'audio/x-wav', 'audio/aac', 'audio/wave',
                    'audio/vnd.wave',
                ]
                _allowed_extensions = {
                    'pdf', 'doc', 'docx',
                    'jpg', 'jpeg', 'png', 'gif', 'bmp', 'webp', 'tiff', 'tif', 'svg',
                    'mp3', 'wav', 'm4a', 'ogg', 'webm', 'aac', 'mpeg',
                }

                # --- Extract files from form data (declared File() params trigger multipart parsing) ---
                upload_parts: List[UploadFile] = []
                form_data = await request.form()
                for key, value in form_data.multi_items():
                    if key in ("attachment", "attachments"):
                        if hasattr(value, "read") and getattr(value, "filename", None):
                            upload_parts.append(value)

                logger.info(
                    "request-service upload: upload_parts=%d, filenames=%s",
                    len(upload_parts),
                    [p.filename for p in upload_parts],
                )

                accum_ctx: Dict[str, Any] = {}
                batch_name_labels: Dict[str, str] = {}

                _VALID_FILE_TYPES = {"pdf", "docx", "image", "voice_note"}
                type_list: list = [
                    t.strip().lower()
                    for t in (file_types or "").split(",")
                    if t.strip()
                ]
                for t in type_list:
                    if t not in _VALID_FILE_TYPES:
                        raise HTTPException(
                            status_code=400,
                            detail=f"Invalid file_types value '{t}'. Each entry must be one of: {', '.join(sorted(_VALID_FILE_TYPES))}",
                        )

                for idx, part in enumerate(upload_parts):
                    ct = part.content_type or ""
                    if ct not in allowed_content_types:
                        ext = (part.filename or "").rsplit(".", 1)[-1].lower() if "." in (part.filename or "") else ""
                        guessed = mimetypes.guess_type(part.filename or "")[0] or ""
                        if ext not in _allowed_extensions and guessed not in allowed_content_types:
                            raise HTTPException(
                                status_code=400,
                                detail="Invalid file type. Only PDF, DOCX documents, images and audio files are allowed."
                            )
                        ct = guessed or ct

                    file_content = await part.read()
                    file_size = len(file_content)

                    if idx < len(type_list):
                        kind = type_list[idx]
                    else:
                        kind = infer_referral_file_type(
                            ct, part.filename, file_content[:32] if file_content else None
                        )
                    is_voice_note = kind == "voice_note"
                    is_document = kind in ("pdf", "docx")
                    is_image = kind == "image"

                    check_duplicate_filename(part.filename or "attachment", {}, batch_name_labels)
                    batch_name_labels[(part.filename or "attachment").lower().strip()] = part.filename or "attachment"
                    page_count = 0
                    duration_seconds = 0

                    if SUBSCRIPTION_FEATURES_ENABLED:
                        attachment_action_type = (
                            UsageActionType.VOICE_NOTE if is_voice_note else UsageActionType.UPLOAD
                        )
                    else:
                        attachment_action_type = 'voice_note' if is_voice_note else 'upload'

                    if SUBSCRIPTION_FEATURES_ENABLED:
                        try:
                            with self.db.get_session() as session:
                                subscription_service = SubscriptionService(session)
                                user_uuid = uuid.UUID(user_id)

                                if kind == "pdf":
                                    try:
                                        import fitz
                                        from io import BytesIO
                                        pdf_doc = fitz.open(stream=file_content, filetype="pdf")
                                        page_count = pdf_doc.page_count
                                        pdf_doc.close()
                                    except Exception as pdf_err:
                                        logger.warning(f"Could not read PDF pages: {pdf_err}")
                                        page_count = 1

                                if is_voice_note:
                                    duration_seconds = int(file_size / 32000)

                                can_perform, blocked_reason, message = subscription_service.can_perform_action(
                                    user_id=user_uuid,
                                    action_type=attachment_action_type,
                                    file_size_bytes=file_size,
                                    page_count=page_count if kind == "pdf" else 0,
                                    is_image=is_image,
                                    duration_seconds=duration_seconds if is_voice_note else 0
                                )

                                if not can_perform:
                                    upgrade_options = subscription_service.get_upgrade_suggestions(
                                        user_uuid, attachment_action_type
                                    )
                                    raise HTTPException(
                                        status_code=402,
                                        detail={
                                            "error": blocked_reason.value if hasattr(blocked_reason, 'value') else str(blocked_reason),
                                            "message": message,
                                            "action_blocked": attachment_action_type.value if hasattr(attachment_action_type, 'value') else str(attachment_action_type),
                                            "suggestions": upgrade_options
                                        }
                                    )
                        except HTTPException:
                            raise
                        except Exception as e:
                            logger.warning(f"Subscription validation failed, allowing upload: {e}")

                    max_size = 10 * 1024 * 1024
                    if is_document:
                        max_size = 25 * 1024 * 1024
                    elif is_voice_note:
                        max_size = 50 * 1024 * 1024
                    elif is_image:
                        max_size = 10 * 1024 * 1024

                    if file_size > max_size:
                        raise HTTPException(
                            status_code=400,
                            detail=f"File size too large. Maximum size is {max_size // (1024*1024)}MB for this file type."
                        )

                    if kind == "pdf":
                        s3_content_type = "application/pdf"
                    elif kind == "docx":
                        s3_content_type = ct if ct in (
                            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                            "application/msword",
                        ) else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                    elif kind == "image":
                        s3_content_type = ct if str(ct).startswith("image/") else (
                            mimetypes.guess_type(part.filename or "")[0] or "application/octet-stream"
                        )
                    elif kind == "voice_note":
                        s3_content_type = ct if str(ct).startswith("audio/") else "application/octet-stream"
                    else:
                        s3_content_type = ct or "application/octet-stream"

                    try:
                        if kind == "pdf":
                            file_extension = "pdf"
                        elif kind == "docx":
                            file_extension = (
                                "docx" if (part.filename or "").lower().endswith(".docx") else "doc"
                            )
                        else:
                            file_extension = part.filename.split(".")[-1] if "." in (part.filename or "") else ""
                        unique_filename = f"service_requests/{user_id}/{uuid.uuid4()}.{file_extension}"
                        s3_client = boto3.client(
                            's3',
                            aws_access_key_id=os.getenv('AWS_ACCESS_KEY_ID'),
                            aws_secret_access_key=os.getenv('AWS_SECRET_ACCESS_KEY'),
                            region_name=os.getenv('AWS_REGION', 'eu-west-2')
                        )
                        s3_client.put_object(
                            Bucket=os.getenv('S3_BUCKET_NAME'),
                            Key=unique_filename,
                            Body=file_content,
                            ContentType=s3_content_type
                        )
                        attachment_url = (
                            f"https://{os.getenv('S3_BUCKET_NAME')}.s3."
                            f"{os.getenv('AWS_REGION', 'eu-west-2')}.amazonaws.com/{unique_filename}"
                        )
                    except Exception as e:
                        logger.error(f"Error uploading attachment: {str(e)}")
                        raise HTTPException(status_code=500, detail="Failed to upload attachment")

                    upload_result = {
                        "url": attachment_url,
                        "s3_key": unique_filename,
                        "original_filename": part.filename,
                        "size": file_size,
                        "content_type": s3_content_type,
                        "content_sha256": content_sha256_hex(file_content).lower(),
                    }
                    if kind == "pdf":
                        pdf_title = try_pdf_document_title(file_content)
                        if pdf_title:
                            upload_result["document_title"] = pdf_title
                    merge_referral_upload_into_context(accum_ctx, kind, upload_result, part.filename)

                    if SUBSCRIPTION_FEATURES_ENABLED:
                        try:
                            with self.db.get_session() as session:
                                subscription_service = SubscriptionService(session)
                                user_uuid = uuid.UUID(user_id)
                                subscription_service.deduct_usage(
                                    user_id=user_uuid,
                                    action_type=attachment_action_type,
                                    file_size_bytes=file_size,
                                    page_count=page_count,
                                    duration_seconds=duration_seconds,
                                    file_name=part.filename,
                                    conversation_id=f"request_service_{preferred_lawyer_id}"
                                )
                                logger.info(f"Deducted usage for {attachment_action_type} from user {user_id}")
                        except Exception as e:
                            logger.warning(f"Failed to deduct usage for request-service attachment: {e}")

                # Create a unique conversation ID for this request
                conversation_id = f"request_service_{uuid.uuid4()}"
                
                with self.db.get_session() as session:
                    # Normalize budget range for storage
                    normalized_budget_range = self._normalize_budget_range(budget_range) if budget_range else None
                    ongoing_budget_gbp = self._ongoing_budget_gbp(consultation_type, budget_range)
                    
                    # Create the referral request
                    referral = ReferralRequest(
                        user_id=uuid.UUID(user_id),
                        conversation_id=conversation_id,
                        legal_area=legal_area,
                        issue_summary=service,  # Use service as issue summary
                        consultation_category=consultation_category,
                        consultation_type=consultation_type,
                        budget_range=normalized_budget_range,
                        ongoing_budget_gbp=ongoing_budget_gbp,
                        casework_service_type=casework_service_type if consultation_category == "casework" else None,
                        casework_budget_gbp=int(casework_budget_gbp) if (consultation_category == "casework" and casework_budget_gbp is not None) else None,
                        urgency_level=urgency_level,
                        user_location=user_location,
                        additional_info=additional_notes or '',
                        confirmation_summary=f"Service request: {service}",
                        user_confirmed=True,
                        status=ReferralStatus.CONFIRMED.value
                    )
                    
                    session.add(referral)
                    session.flush()  # Get the ID
                    
                    if accum_ctx:
                        referral.voice_note_url = accum_ctx.get("voice_note_url")
                        referral.pdf_attachment_url = accum_ctx.get("pdf_attachment_url")
                        referral.docx_attachment_url = accum_ctx.get("docx_attachment_url")
                        referral.image_attachment_url = accum_ctx.get("image_attachment_url")
                        referral.attachment_metadata = build_referral_attachment_metadata(accum_ctx)
                    
                    # If user specified a preferred lawyer, match with them
                    if preferred_lawyer_id:
                        try:
                            # Validate lawyer exists and is actually a lawyer
                            lawyer = session.query(User).filter(
                                User.id == uuid.UUID(preferred_lawyer_id),
                                User.professional_type == 'Lawyer'
                            ).first()
                            
                            if not lawyer:
                                raise HTTPException(status_code=400, detail="Selected lawyer not found")

                            booking = None
                            if requires_booking_slot(consultation_category):
                                booking_service = BookingService(session)
                                if not booking_service.validate_slot(
                                    lawyer.id,
                                    requested_start_at=requested_start_at,
                                    requested_end_at=requested_end_at,
                                    consultation_type=consultation_type,
                                ):
                                    raise HTTPException(
                                        status_code=409,
                                        detail="Selected booking slot is no longer available.",
                                    )
                                booking = booking_service.create_booking_request(
                                    user_id=uuid.UUID(user_id),
                                    professional_id=lawyer.id,
                                    referral_request_id=referral.id,
                                    consultation_category=consultation_category,
                                    consultation_type=consultation_type,
                                    requested_start_at=requested_start_at,
                                    requested_end_at=requested_end_at,
                                    timezone_name=booking_timezone or "Europe/London",
                                )
                            
                            # Create recommendation record for the selected lawyer
                            recommendation = LawyerRecommendation(
                                referral_request_id=referral.id,
                                lawyer_id=lawyer.id,
                                is_best_suited=True,
                                match_score=100,  # Perfect match since user selected
                                match_reasons=["Selected by user from search results"],
                                recommendation_rank=1
                            )
                            session.add(recommendation)
                            
                            # Update referral with matched lawyer
                            referral.matched_lawyer_id = lawyer.id
                            referral.status = ReferralStatus.MATCHED.value
                            
                            # Add to lawyer's inbox
                            inbox_item = LawyerInbox(
                                lawyer_id=lawyer.id,
                                referral_request_id=referral.id,
                                status='pending',
                                priority_score=70 if urgency_level == 'urgent' else 50
                            )
                            session.add(inbox_item)
                            
                            user_record = session.query(User).filter(User.id == uuid.UUID(user_id)).first()

                            self._attach_service_request_manuscript(
                                referral=referral,
                                lawyer_id=lawyer.id,
                                user_record=user_record,
                                session=session,
                            )
                            
                            session.commit()

                            try:
                                from services.email import EmailService
                                EmailService.send_request_service_submitted_email(
                                    to_email=user_record.email if user_record else None,
                                    first_name=user_record.first_name if user_record else None,
                                    lawyer_name=f"{lawyer.first_name} {lawyer.last_name}",
                                    urgency_level=referral.urgency_level,
                                )
                            except Exception as e:
                                logger.error(f"Failed to send request-service-submitted email for referral {referral.id}: {e}")

                            # Send push notification to the lawyer
                            try:
                                # Get the user's name for the notification
                                client_name = f"{user_record.first_name} {user_record.last_name}" if user_record else "A client"
                                
                                # Get lawyer's device tokens
                                lawyer_devices = session.query(DeviceToken).filter(
                                    DeviceToken.user_id == lawyer.id,
                                    DeviceToken.is_active == True
                                ).all()
                                
                                if lawyer_devices:
                                    device_tokens = [device.device_token for device in lawyer_devices]
                                    await self.firebase_service.send_new_referral_notification(
                                        device_tokens=device_tokens,
                                        client_name=client_name,
                                        referral_id=str(referral.id),
                                        user_id=user_id,
                                        legal_area=referral.legal_area
                                    )
                                    logger.info(f"Sent new referral notification to lawyer {preferred_lawyer_id} from request-service endpoint")

                                # Persist in-app notification so the lawyer sees it in /notifications/list
                                legal_area_str = referral.legal_area or "legal matter"
                                NotificationService(session).create(
                                    recipient_id=lawyer.id,
                                    title="New Client Request!",
                                    body=f"{client_name} has sent you a referral request for {legal_area_str}.",
                                    notification_type="new_referral",
                                    data={
                                        "referral_id": str(referral.id),
                                        "user_id": user_id,
                                        "screen": "LawyerInbox",
                                    },
                                )
                                session.commit()
                            except Exception as notif_err:
                                logger.warning(f"Failed to send notification for request-service: {notif_err}")
                            
                            uploaded_files = (
                                (referral.attachment_metadata or {}).get("files") or []
                            )
                            return {
                                "success": True,
                                "message": f"Your service request has been sent to {lawyer.first_name} {lawyer.last_name}. You'll be notified when they respond.",
                                "referral_id": str(referral.id),
                                "lawyer_name": f"{lawyer.first_name} {lawyer.last_name}",
                                "status": "sent_to_lawyer",
                                "attachment_uploaded": bool(accum_ctx),
                                "attachment_count": len(uploaded_files),
                                "booking": {
                                    "id": str(booking.id),
                                    "status": booking.status,
                                    "start_at": ensure_utc(booking.requested_start_at).isoformat(),
                                    "end_at": ensure_utc(booking.requested_end_at).isoformat(),
                                    "timezone": booking.timezone,
                                } if booking else None,
                                "attachments": [
                                    {
                                        "type": f.get("kind"),
                                        "url": f.get("url"),
                                        "name": f.get("original_filename"),
                                    }
                                    for f in uploaded_files
                                ],
                            }
                            
                        except ValueError:
                            raise HTTPException(status_code=400, detail="Invalid lawyer ID format")
                    
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error in request service: {str(e)}")
                raise HTTPException(status_code=500, detail="Failed to process service request")
        
        return router 
    
    # Helper methods for request service functionality
    def _attach_service_request_manuscript(
        self,
        *,
        referral: ReferralRequest,
        lawyer_id: uuid.UUID,
        user_record: Optional[User],
        session,
    ) -> None:
        """Generate and attach a direct request-service manuscript."""
        try:
            client_name = (
                f"{user_record.first_name} {user_record.last_name}".strip()
                if user_record
                else "Unknown Client"
            )
            pdf_metadata = {
                "client_name": client_name,
                "referral_id": str(referral.id),
                "legal_area": self._humanize_service_value(referral.legal_area),
                "consultation_type": get_consultation_type_display(referral.consultation_type),
                "urgency_level": referral.urgency_level or "Not specified",
                "date_started": referral.created_at.strftime('%B %d, %Y at %H:%M UTC') if referral.created_at else "",
            }
            pdf_bytes = self.pdf_service.generate_service_request_pdf(
                request_details=self._service_request_manuscript_details(referral, session=session),
                referral_metadata=pdf_metadata,
            )

            display_file_name = "Request Service Manuscript.pdf"
            s3_metadata = self.s3_service.upload_file(
                file_content=pdf_bytes,
                file_name=display_file_name,
                content_type="application/pdf",
                referral_id=str(referral.id),
                file_type="service_request",
            )
            if not s3_metadata:
                logger.warning("Request-service manuscript upload failed for referral %s", referral.id)
                return

            s3_metadata["content_sha256"] = content_sha256_hex(pdf_bytes).lower()
            s3_metadata["document_title"] = "Request Service Manuscript"
            self._append_service_request_manuscript_metadata(
                referral=referral,
                upload_result=s3_metadata,
                display_file_name=display_file_name,
            )

            manuscript_message = InboxMessage(
                referral_request_id=referral.id,
                sender_id=referral.user_id,
                sender_type='user',
                message_content="Please review the attached PDF containing the client's selected service request details.",
                message_type='service_request_manuscript',
                file_url=s3_metadata.get("url"),
                file_name=display_file_name,
                file_type='application/pdf',
                file_size=s3_metadata.get("file_size"),
                reply_to_message_id=None,
            )
            session.add(manuscript_message)
            logger.info("Request-service manuscript PDF sent to lawyer %s for referral %s", lawyer_id, referral.id)
        except Exception as exc:
            logger.warning("Failed to attach request-service manuscript for referral %s: %s", referral.id, exc)

    def _append_service_request_manuscript_metadata(
        self,
        *,
        referral: ReferralRequest,
        upload_result: Dict[str, Any],
        display_file_name: str,
    ) -> None:
        attachment_metadata = dict(referral.attachment_metadata or {})
        files = list(attachment_metadata.get("files") or [])
        s3_key = upload_result.get("s3_key")
        if not any((f.get("metadata") or {}).get("s3_key") == s3_key for f in files if isinstance(f, dict)):
            files.append({
                "kind": "pdf",
                "url": upload_result.get("url"),
                "original_filename": display_file_name,
                "metadata": upload_result,
                "content_sha256": upload_result.get("content_sha256"),
            })
        attachment_metadata["files"] = files

        if not referral.pdf_attachment_url:
            referral.pdf_attachment_url = upload_result.get("url")
            attachment_metadata["pdf"] = upload_result

        referral.attachment_metadata = attachment_metadata

    def _service_request_manuscript_details(self, referral: ReferralRequest, *, session=None) -> List[Dict[str, Any]]:
        details = [
            {"label": "Selected Service", "value": self._humanize_service_value(referral.legal_area)},
            {"label": "Issue Summary", "value": self._humanize_service_value(referral.issue_summary)},
            {"label": "Consultation Category", "value": get_consultation_duration_display(referral.consultation_category)},
            {"label": "Consultation Type", "value": get_consultation_type_display(referral.consultation_type)},
            {"label": "Budget", "value": get_budget_range_display(referral.budget_range)},
            {"label": "Urgency", "value": get_urgency_level_display(referral.urgency_level)},
            {"label": "Location", "value": referral.user_location},
            {"label": "Additional Notes", "value": referral.additional_info},
        ]

        if referral.casework_service_type:
            details.insert(4, {
                "label": "Casework Service",
                "value": CASEWORK_LABEL_BY_SERVICE.get(
                    referral.casework_service_type,
                    self._humanize_service_value(referral.casework_service_type),
                ),
            })
        if referral.casework_budget_gbp is not None:
            details.insert(5, {
                "label": "Casework Budget",
                "value": f"£{int(referral.casework_budget_gbp):,}",
            })

        selected_slot = self._service_request_booking_slot_display(referral, session=session)
        if selected_slot:
            details.insert(4, {
                "label": "Selected Slot",
                "value": selected_slot,
            })

        return details

    def _service_request_booking_slot_display(self, referral: ReferralRequest, *, session=None) -> Optional[str]:
        if session is None:
            return None
        booking = session.query(ConsultationBookingRequest).filter(
            ConsultationBookingRequest.referral_request_id == referral.id
        ).first()
        if not booking:
            return None

        start_at = ensure_utc(booking.requested_start_at)
        end_at = ensure_utc(booking.requested_end_at)
        timezone_name = booking.timezone or "UTC"
        try:
            from zoneinfo import ZoneInfo
            tzinfo = ZoneInfo(timezone_name)
            start_local = start_at.astimezone(tzinfo)
            end_local = end_at.astimezone(tzinfo)
        except Exception:
            start_local = start_at
            end_local = end_at
            timezone_name = "UTC"

        return (
            f"{start_local.strftime('%a, %d %b %Y')}, "
            f"{start_local.strftime('%H:%M')}-{end_local.strftime('%H:%M')} "
            f"({timezone_name})"
        )

    def _humanize_service_value(self, value: Optional[str]) -> str:
        if not value:
            return "Not specified"
        return str(value).replace("_", " ").strip().title()

    def _format_service_request_message(self, referral: ReferralRequest) -> str:
        """Format service request details into a readable message for lawyers"""
        urgency_map = {
            "urgent": "Urgent (within 24 hours)",
            "within_week": "Within a week",
            "within_month": "Within a month",
            "not_urgent": "Not urgent"
        }
        
        urgency_display = urgency_map.get(referral.urgency_level, referral.urgency_level)
        
        message_parts = [
            "=== NEW SERVICE REQUEST ===\n",
            f"Legal Area: {referral.legal_area}",
            f"Issue Summary: {referral.issue_summary}",
            f"Consultation Type: {referral.consultation_category}",
        ]
        
        if referral.consultation_type:
            message_parts.append(f"Specific Type: {referral.consultation_type}")
        
        if referral.budget_range:
            message_parts.append(f"Budget Range: {referral.budget_range}")
        
        message_parts.extend([
            f"Urgency: {urgency_display}",
        ])
        
        if referral.user_location:
            message_parts.append(f"Location: {referral.user_location}")
        
        if referral.additional_info:
            message_parts.append(f"Additional Notes: {referral.additional_info}")
        
        meta = referral.attachment_metadata or {}
        attachment_urls = []
        if meta.get("files"):
            for f in meta["files"]:
                u = f.get("url") or (f.get("metadata") or {}).get("url")
                if u:
                    attachment_urls.append(u)
        else:
            for u in (
                referral.voice_note_url,
                referral.pdf_attachment_url,
                referral.docx_attachment_url,
                referral.image_attachment_url,
            ):
                if u:
                    attachment_urls.append(u)
        if attachment_urls:
            message_parts.append(
                "Attachments:\n" + "\n".join(f" - {u}" for u in attachment_urls)
            )
        
        message_parts.extend([
            "",
            "This request was submitted directly through the search interface.",
            "Please review and respond to accept or decline this request."
        ])
        
        return "\n".join(message_parts)
    
    async def _generate_service_request_recommendations(self, referral_id: uuid.UUID, legal_area: str, session) -> List[Dict]:
        """Generate lawyer recommendations for service requests"""
        # Map legal areas to expertise format (same as in referral.py)
        area_mapping = {
            'immigration': 'Immigration Law',
            'family': 'Family Law',
            'employment': 'Employment Law',
            'road_traffic': 'Road Traffic Law',
            'criminal': 'Criminal Law',
            'property': 'Property Law',
            'corporate': 'Corporate Law',
            'personal_injury': 'Personal Injury'
        }
        
        expertise_area = area_mapping.get(legal_area.lower(), legal_area.title() + ' Law')
        
        # Find lawyers with matching expertise
        lawyers_query = session.query(User).filter(
            User.professional_type == 'Lawyer',
            User.legal_expertise.contains([expertise_area])
        ).limit(10)  # Get more than needed for scoring
        
        lawyers = lawyers_query.all()
        
        if not lawyers:
            # Fallback: get any lawyers if no expertise match
            lawyers = session.query(User).filter(
                User.professional_type == 'Lawyer'
            ).limit(3).all()
        
        # Score and rank lawyers (simplified version of referral.py logic)
        scored_lawyers = []
        for lawyer in lawyers:
            score = 50  # Base score
            
            # Exact expertise match
            if lawyer.legal_expertise and expertise_area in lawyer.legal_expertise:
                score += 30
            
            # Multiple expertise areas
            if lawyer.legal_expertise and len(lawyer.legal_expertise) > 1:
                score += 10
            
            # Has professional employer
            if lawyer.professional_employer:
                score += 10
            
            # Has about section
            if lawyer.about:
                score += 5
            
            # Add some randomization
            import random
            score += random.randint(-5, 5)
            
            match_reasons = []
            if lawyer.legal_expertise and expertise_area in lawyer.legal_expertise:
                match_reasons.append(f"Specializes in {expertise_area}")
            if lawyer.professional_employer:
                match_reasons.append(f"Works at {lawyer.professional_employer}")
            if not match_reasons:
                match_reasons.append("Qualified legal professional")
            
            scored_lawyers.append({
                'lawyer_id': str(lawyer.id),
                'name': f"{lawyer.first_name} {lawyer.last_name}",
                'firm': lawyer.professional_employer,
                'score': min(100, max(0, score)),
                'reasons': match_reasons
            })
        
        # Sort by score and take top 3
        scored_lawyers.sort(key=lambda x: x['score'], reverse=True)
        top_lawyers = scored_lawyers[:3]
        
        # Create recommendation records for all top lawyers
        for i, lawyer_data in enumerate(top_lawyers):
            is_best_suited = (i == 0)
            recommendation = LawyerRecommendation(
                referral_request_id=referral_id,
                lawyer_id=uuid.UUID(lawyer_data['lawyer_id']),
                is_best_suited=is_best_suited,
                match_score=lawyer_data['score'],
                match_reasons=lawyer_data['reasons'],
                recommendation_rank=i + 1
            )
            session.add(recommendation)
        
        return top_lawyers

    # Helper methods for search functionality
    def _get_display_name(self, user) -> str:
        """Get the appropriate display name for the professional"""
        try:
            if user.firm_name:
                return user.firm_name
            elif user.organisation_name:
                return user.organisation_name
            elif user.company_name:
                return user.company_name
            elif user.first_name and user.last_name:
                return f"{user.first_name} {user.last_name}"
            elif user.first_name:
                return user.first_name
            else:
                return "Professional"
        except Exception:
            return "Professional"
    
    def _get_primary_expertise(self, user) -> str:
        """Get the primary area of expertise for display"""
        try:
            if user.legal_expertise and len(user.legal_expertise) > 0:
                return user.legal_expertise[0]
            elif user.practice_areas and len(user.practice_areas) > 0:
                return user.practice_areas[0]
            elif user.areas_of_legal_focus and len(user.areas_of_legal_focus) > 0:
                return user.areas_of_legal_focus[0]
            else:
                return user.professional_type or "Legal Professional"
        except Exception:
            return "Legal Professional"
    
    def _get_location_display(self, user) -> str:
        """Get location information for display"""
        try:
            location_parts = []
            if user.town_city:
                location_parts.append(user.town_city)
            if user.country and user.country != user.town_city:
                location_parts.append(user.country)
            
            if location_parts:
                return ", ".join(location_parts)
            elif user.firm_address:
                return user.firm_address
            else:
                return "Location not specified"
        except Exception:
            return "Location not specified"
    
    def _calculate_search_relevance(self, user, search_query: str, practice_area: Optional[str] = None) -> float:
        """Calculate search relevance score (0.0 to 1.0)"""
        try:
            score = 0.0
            query_lower = search_query.lower()
            
            # Name matches (highest weight)
            if user.first_name and query_lower in user.first_name.lower():
                score += 0.4
            if user.last_name and query_lower in user.last_name.lower():
                score += 0.4
            if user.firm_name and query_lower in user.firm_name.lower():
                score += 0.5
            if user.organisation_name and query_lower in user.organisation_name.lower():
                score += 0.5
            if user.company_name and query_lower in user.company_name.lower():
                score += 0.5
            
            # Expertise matches (medium weight)
            if user.legal_expertise:
                for expertise in user.legal_expertise:
                    if query_lower in expertise.lower():
                        score += 0.3
            if user.practice_areas:
                for area in user.practice_areas:
                    if query_lower in area.lower():
                        score += 0.3
            if user.areas_of_legal_focus:
                for area in user.areas_of_legal_focus:
                    if query_lower in area.lower():
                        score += 0.3
            
            # Services matches (medium weight)
            if user.services:
                for service in user.services:
                    if query_lower in service.lower():
                        score += 0.25
            
            # About/tagline matches (lower weight)
            if user.about and query_lower in user.about.lower():
                score += 0.15
            if user.tagline and query_lower in user.tagline.lower():
                score += 0.15
            
            # Practice area exact match bonus
            if practice_area:
                if user.legal_expertise and practice_area in user.legal_expertise:
                    score += 0.2
                if user.practice_areas and practice_area in user.practice_areas:
                    score += 0.2
                if user.areas_of_legal_focus and practice_area in user.areas_of_legal_focus:
                    score += 0.2
            
            # Cap the score at 1.0
            return min(score, 1.0)
            
        except Exception:
            return 0.0
    
    def _get_available_practice_areas(self) -> List[str]:
        """Get list of available practice areas from database - ONLY the four areas LawNeeds offers"""
        # ONLY the four areas LawNeeds offers
        allowed_areas = {
            "Immigration law",
            "Immigration Law", 
            "Employment law",
            "Employment Law",
            "Family law",
            "Family Law",
            "Road Traffic Offences",
            "Road Traffic Law",
            "Traffic Offences"
        }
        
        try:
            with self.db.get_session() as session:
                # Get unique practice areas from all professionals
                areas = set()
                
                # Query legal_expertise
                expertise_results = session.query(User.legal_expertise).filter(
                    User.professional_type.in_(["Lawyer", "Law Firm", "A2J Partners", "HR/Legal (SME)"]),
                    User.legal_expertise.isnot(None)
                ).all()
                
                for result in expertise_results:
                    if result.legal_expertise:
                        # Only add areas that are in the allowed list
                        areas.update([area for area in result.legal_expertise if area in allowed_areas])
                
                # Query practice_areas
                practice_results = session.query(User.practice_areas).filter(
                    User.professional_type.in_(["Lawyer", "Law Firm", "A2J Partners", "HR/Legal (SME)"]),
                    User.practice_areas.isnot(None)
                ).all()
                
                for result in practice_results:
                    if result.practice_areas:
                        # Only add areas that are in the allowed list
                        areas.update([area for area in result.practice_areas if area in allowed_areas])
                
                # Query areas_of_legal_focus
                focus_results = session.query(User.areas_of_legal_focus).filter(
                    User.professional_type.in_(["Lawyer", "Law Firm", "A2J Partners", "HR/Legal (SME)"]),
                    User.areas_of_legal_focus.isnot(None)
                ).all()
                
                for result in focus_results:
                    if result.areas_of_legal_focus:
                        # Only add areas that are in the allowed list
                        areas.update([area for area in result.areas_of_legal_focus if area in allowed_areas])
                
                return sorted(list(areas))
                
        except Exception as e:
            logger.error(f"Error getting practice areas: {str(e)}")
            # Return ONLY the four practice areas LawNeeds offers if database query fails
            return [
                "Immigration law",
                "Employment law", 
                "Family law",
                "Road Traffic Offences"
            ]
    
    def _get_common_services(self) -> List[str]:
        """Get list of common services from database"""
        try:
            with self.db.get_session() as session:
                # Get unique services from all professionals
                services = set()
                
                service_results = session.query(User.services).filter(
                    User.professional_type.in_(["Lawyer", "Law Firm", "A2J Partners", "HR/Legal (SME)"]),
                    User.services.isnot(None)
                ).all()
                
                for result in service_results:
                    if result.services:
                        services.update(result.services)
                
                return sorted(list(services))
                
        except Exception as e:
            logger.error(f"Error getting services: {str(e)}")
            # Return default services if database query fails
            return [
                "Legal consultation",
                "Document review",
                "Legal representation",
                "Contract drafting",
                "Legal advice",
                "Court representation",
                "Mediation",
                "Legal research"
            ] 
