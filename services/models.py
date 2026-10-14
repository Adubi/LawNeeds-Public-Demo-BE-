from sqlalchemy import (
    Column,
    Integer,
    String,
    Text,
    DateTime,
    ForeignKey,
    UUID as SA_UUID,
    Date,
    Table,
    func,
    Enum,
    Boolean,
    JSON,
    Numeric,
    UniqueConstraint,
    Float,
    Index,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID, ARRAY
from sqlalchemy.orm import relationship  
import uuid
from datetime import datetime, timezone
from sqlalchemy.ext.declarative import declarative_base
import enum

Base = declarative_base()

# Association table for User and KnowledgeBase
# Note: Without relationships, this table is just a bridge and won't be managed by ORM relationships
user_knowledgebase = Table(
    'user_knowledgebase',
    Base.metadata,
    Column('user_id', PGUUID(as_uuid=True), ForeignKey('users.id', ondelete='CASCADE'), primary_key=True),
    Column('knowledge_base_id', Integer, ForeignKey('knowledge_base.id', ondelete='CASCADE'), primary_key=True)
)

# Enum for the professional user types
class ProfessionalType(enum.Enum):
    LAWYER = "Lawyer"
    LAW_FIRM = "Law Firm"
    HR_LEGAL = "HR/Legal (SME)"
    A2J_PARTNERS = "A2J Partners"
    NONE = "None"  # For regular users

# Enum for legal practice areas
class PracticeArea(enum.Enum):
    IMMIGRATION = "Immigration law"
    EMPLOYMENT = "Employment law"
    TRAFFIC = "Road Traffic Offences"
    FAMILY = "Family law"

# Enum for HR/Legal selection
class HRLegalType(enum.Enum):
    HR = "HR"
    LEGAL = "Legal"
    BOTH = "Both"

# Enum for SRA/BSB selection
class RegulatoryBody(enum.Enum):
    SRA = "SRA"
    BSB = "BSB"
    IAA = "IAA"  # Immigration Advisers Authority
    NONE = "None"

# Enum for proof of ID document types
class ProofOfIDType(enum.Enum):
    IDENTITY_CARD = "Identity Card"
    PASSPORT = "Passport"
    DRIVERS_LICENSE = "Driver's License"

# Enum for A2J Partners organisation types
class A2JOrganisationType(enum.Enum):
    LEGAL_AID = "Legal Aid Organisation"
    LAW_CENTRE = "Law Centre"
    PRO_BONO = "Pro-Bono Clinic"

# Legal services that lawyers can offer
LEGAL_SERVICES = [
    "Immigration Law",
    "Family Law", 
    "Employment Law",
    "Criminal Law",
    "Corporate Law",
    "Property Law",
    "Personal Injury",
    "Intellectual Property",
    "Tax Law",
    "Contract Law",
    "Litigation",
    "Mediation",
    "Arbitration",
    "Legal Consultation",
    "Document Review",
    "Legal Research",
    "Compliance",
    "Mergers & Acquisitions",
    "Banking & Finance",
    "Insurance Law",
    "Environmental Law",
    "Human Rights Law",
    "International Law",
    "Administrative Law",
    "Constitutional Law"
]

# Legal skills that lawyers can have
LEGAL_SKILLS = [
    "Legal Research",
    "Legal Writing",
    "Case Analysis",
    "Contract Drafting",
    "Negotiation",
    "Client Counseling",
    "Court Advocacy",
    "Mediation",
    "Arbitration",
    "Due Diligence",
    "Compliance Review",
    "Risk Assessment",
    "Legal Strategy",
    "Cross-examination",
    "Brief Writing",
    "Deposition",
    "Discovery",
    "Settlement Negotiation",
    "Regulatory Analysis",
    "International Law",
    "Intellectual Property",
    "Mergers & Acquisitions",
    "Corporate Governance",
    "Employment Relations",
    "Immigration Procedures"
]

# Enum for chat response feedback types
class FeedbackType(enum.Enum):
    THUMBS_UP = "thumbs_up"
    THUMBS_DOWN = "thumbs_down"

# Updated Enum for consultation types based on new requirements
class ConsultationType(enum.Enum):
    # Single consultation options
    SINGLE_5_MIN = "single_5_min"
    SINGLE_10_MIN = "single_10_min"
    SINGLE_20_MIN = "single_20_min"
    SINGLE_30_MIN = "single_30_min"
    SINGLE_1_HOUR = "single_1_hour"
    
    # Ongoing support options
    ONGOING_3_DAY = "ongoing_3_day"
    ONGOING_5_DAY = "ongoing_5_day"
    
    # Casework option
    CASEWORK = "casework"
    
    # For users who are not sure yet
    NOT_SURE = "not_sure"

# Updated Enum for consultation budget ranges based on new requirements
class BudgetRange(enum.Enum):
    BELOW_300 = "below_300"
    BETWEEN_300_499 = "300_to_499"
    BETWEEN_500_699 = "500_to_699"
    ABOVE_700 = "above_700"

# New enum for main consultation categories
class ConsultationCategory(enum.Enum):
    SINGLE_CONSULTATION = "single_consultation"
    ONGOING_SUPPORT = "ongoing_support"
    CASEWORK = "casework"
    NOT_SURE = "not_sure"


# Casework service types — matches the Service & Price column shown to users
# under the Casework path. Pricing data + per-service budget floors live in
# services.referral_consultation_pricing_display.
class CaseworkServiceType(enum.Enum):
    SKILLED_WORKER_VISA = "skilled_worker_visa"
    STUDENT_VISA = "student_visa"
    VISIT_VISA = "visit_visa"
    ANCESTRY_VISA = "ancestry_visa"
    SPOUSE_PARTNER_VISA = "spouse_partner_visa"
    PBS_DEPENDANTS = "pbs_dependants"
    FAMILY_REUNION = "family_reunion"
    ILR_10_YEAR = "ilr_10_year"
    NATURALISATION = "naturalisation"
    EU_SETTLEMENT_SCHEME = "eu_settlement_scheme"
    FLR_FP_HUMAN_RIGHTS = "flr_fp_human_rights"
    ASYLUM_FULL_SUPPORT = "asylum_full_support"
    FULL_CASE_MANAGEMENT = "full_case_management"


class ConsultationPaymentPricingType(enum.Enum):
    IN_HOUSE = "in_house"
    CUSTOM = "custom"
    CASEWORK = "casework"


class ConsultationPaymentStatus(enum.Enum):
    PENDING = "pending"
    PAID = "paid"
    TRANSFERRED = "transferred"
    REFUNDED = "refunded"
    CANCELLED = "cancelled"


class ConsultationExtensionStatus(enum.Enum):
    DRAFT = "draft"
    PENDING_LAWYER = "pending_lawyer"
    ACCEPTED = "accepted"
    PROPOSED = "proposed"
    DENIED = "denied"
    EXPIRED = "expired"
    PAYMENT_PENDING = "payment_pending"
    PAID = "paid"
    APPLIED = "applied"
    CANCELLED = "cancelled"


class ConsultationBookingStatus(enum.Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    COMPLETED = "completed"

# Enhanced enum for urgency levels with AI inference capabilities
class UrgencyLevel(enum.Enum):
    URGENT = "urgent"          # Inferred from: 'ASAP', 'urgent', 'immediately', 'right away', 'today', 'court tomorrow'
    NORMAL = "normal"
    FLEXIBLE = "flexible"
    INFERRED_URGENT = "inferred_urgent"  # AI-detected urgency from language/tone

# Enum for thumbs down reasons in chat feedback
class ThumbsDownReason(enum.Enum):
    INCORRECT_INFO = "incorrect_info"
    NOT_HELPFUL = "not_helpful"
    TOO_GENERIC = "too_generic"
    CONFUSING = "confusing"
    OTHER = "other"

# Enum for referral request status
class ReferralStatus(enum.Enum):
    PENDING = "pending"              # Initial state when referral is created
    CONFIRMED = "confirmed"          # User has confirmed the referral details
    MATCHING = "matching"            # System is finding a suitable lawyer
    MATCHED = "matched"              # Lawyer has been matched
    ACTIVE = "active"                # Session is active between user and lawyer
    COMPLETED = "completed"          # Session has been completed
    CANCELLED = "cancelled"          # Referral was cancelled
    EXPIRED = "expired"              # Referral expired without being fulfilled

class AppSetting(Base):
    __tablename__ = 'app_setting'

    id = Column(Integer, primary_key=True)
    client = Column(Text)
    name = Column(Text)
    value = Column(Text)

    def __repr__(self):
        return f"<AppSetting(id={self.id}, client={self.client}, name={self.name})>"


class Log(Base):
    __tablename__ = 'log'

    id = Column(Integer, primary_key=True)
    client = Column(Text)
    message = Column(Text)
    createdDate = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    def __repr__(self):
        return f"<Log(id={self.id}, client={self.client}, message={self.message}, createdDate={self.createdDate})>"


class WorkExperience(Base):
    __tablename__ = 'work_experiences'

    id = Column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(PGUUID(as_uuid=True), ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    company_name = Column(String, nullable=False)
    achievement = Column(Text, nullable=True)
    start_date = Column(Date, nullable=True)  # Start date of work experience
    end_date = Column(Date, nullable=True)    # End date of work experience (null for current positions)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    def __repr__(self):
        return f"<WorkExperience(id={self.id}, user_id={self.user_id}, company_name={self.company_name})>"


class User(Base):
    __tablename__ = 'users'

    id = Column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    first_name = Column(String)
    last_name = Column(String)
    user_name = Column(String, unique=True)
    email = Column(String, unique=True)
    hashed_password = Column(String)
    client = Column(String)
    date_of_birth = Column(Date, nullable=True)
    address = Column(String)
    town_city = Column(String)
    country = Column(String)
    postcode = Column(String)
    profile_picture_path = Column(String, nullable=True)  # Path to the stored profile picture
    created_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
        nullable=False,
    )
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    last_seen_at = Column(DateTime(timezone=True), nullable=True)  # Updated on each verified request; used for re-engagement emails
    we_miss_you_14d_sent_at = Column(DateTime(timezone=True), nullable=True)  # Last time the 14-day "We Miss You" email was sent for the current inactivity period
    inactive_user_push_sent_at = Column(DateTime(timezone=True), nullable=True)  # Last time the 14-day inactive_user push notification was sent for the current inactivity period
    profile_incomplete_sent_at = Column(DateTime(timezone=True), nullable=True)  # Last time the profile_incomplete notification was sent (fires once only, never cleared)
    welcome_back_in_app_sent_at = Column(DateTime(timezone=True), nullable=True)  # Last time the welcome_back in-app notification row was created for the current inactivity period
    response_rate_warning_sent_at = Column(DateTime(timezone=True), nullable=True)  # Last time the response_rate_warning was sent; cleared when rate recovers above threshold, so a fresh drop re-fires
    
    
    
    # New fields for all users
    about = Column(Text, nullable=True)  # About section for all users
    tagline = Column(String, nullable=True)  # Tagline for all users
    phone_number = Column(String, nullable=True)  # Phone number for all users
    immigration_status = Column(String, nullable=True)  # Client UK immigration status for personalisation
    webinar = Column(Boolean, default=False, server_default="false", nullable=False)  # Webinar/email opt-in
    
    # Professional user type fields
    professional_type = Column(String, nullable=True)  # Using String instead of Enum for flexibility
    is_search_visible = Column(Boolean, default=True, server_default="true", nullable=False)
    
    # Lawyer specific fields
    sra_bsb_iaa = Column(String, nullable=True)  # LPC/BPC license number
    professional_employer = Column(String, nullable=True)
    legal_expertise = Column(ARRAY(String), nullable=True)  # Store selected practice areas as array
    proof_of_id = Column(String, nullable=True)  # Path to uploaded ID document
    proof_of_id_type = Column(String, nullable=True)  # Type of ID document (passport, identity card, etc.)
    services = Column(ARRAY(String), nullable=True)  # Legal services offered by lawyers
    skills = Column(ARRAY(String), nullable=True)  # Legal skills of lawyers
    hourly_rate = Column(Numeric(10, 2), nullable=True)  # Hourly rate in pounds (£)
    
    # Law Firm specific fields
    firm_name = Column(String, nullable=True)
    date_of_incorporation = Column(Date, nullable=True)
    sra_bsb = Column(String, nullable=True)  # Regulatory body (SRA/BSB/IAA)
    practice_areas = Column(ARRAY(String), nullable=True)  # Store selected practice areas as array
    contact_person = Column(String, nullable=True)  # Managing partner or contact person
    company_registration_number = Column(String, nullable=True)
    # New fields for law firms
    firm_address = Column(String, nullable=True)  # Separate address field for law firms
    firm_website = Column(String, nullable=True)  # Website for law firms
    firm_phone_number = Column(String, nullable=True)  # Phone number for law firms
    firm_hourly_rate = Column(Numeric(10, 2), nullable=True)  # Hourly rate in pounds (£) for law firms
    
    # HR/Legal (SME) specific fields
    hr_legal_type = Column(String, nullable=True)  # HR/Legal/Both
    sector = Column(String, nullable=True)
    company_name = Column(String, nullable=True)
    
    # A2J Partners specific fields
    organisation_name = Column(String, nullable=True)
    date_of_establishment = Column(Date, nullable=True)
    organisation_type = Column(String, nullable=True)  # Legal Aid Organisation, Law Centre, Pro-Bono Clinic
    areas_of_legal_focus = Column(ARRAY(String), nullable=True)  # Store selected practice areas as array
    registration_charity_number = Column(String, nullable=True)  # Registration/Charity number (used as proof of ID)
    # New field for A2J Partners
    a2j_website = Column(String, nullable=True)  # Website for A2J Partners
    
    # OAuth fields
    oauth_provider = Column(String, nullable=True)  # 'google', 'apple', or None for regular users
    oauth_id = Column(String, nullable=True)  # Provider-specific user ID
    is_email_verified = Column(Boolean, default=False)  # For OAuth users, this is automatically True
    
    # Stripe Connect marketplace fields for lawyers
    stripe_connect_account_id = Column(String(100), nullable=True)
    stripe_connect_onboarded = Column(Boolean, default=False)

    def __repr__(self):
        return f"<User(id={self.id}, user_name={self.user_name}, email={self.email}, professional_type={self.professional_type})>"


class IdentityVerification(Base):
    __tablename__ = 'identity_verifications'
    __table_args__ = (
        Index(
            'ix_identity_verifications_user_provider_created_at',
            'user_id',
            'provider',
            'created_at',
        ),
    )

    id = Column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(PGUUID(as_uuid=True), ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    provider = Column(String(30), nullable=False, default='persona')
    inquiry_id = Column(String(100), nullable=False, unique=True, index=True)
    template_id = Column(String(100), nullable=False)
    environment = Column(String(30), nullable=False)
    account_type = Column(String(30), nullable=False)
    status = Column(String(30), nullable=False, default='not_started', index=True)
    provider_status = Column(String(50), nullable=True)
    approved_at = Column(DateTime, nullable=True)
    declined_at = Column(DateTime, nullable=True)
    last_event_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )


class PersonaWebhookEvent(Base):
    __tablename__ = 'persona_webhook_events'

    id = Column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_id = Column(String(100), nullable=False, unique=True, index=True)
    event_name = Column(String(100), nullable=False)
    inquiry_id = Column(String(100), nullable=True, index=True)
    event_created_at = Column(DateTime, nullable=True)
    payload = Column(JSON, nullable=False)
    processed_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)


class KnowledgeBase(Base):
    __tablename__ = 'knowledge_base'

    id = Column(Integer, primary_key=True)
    client = Column(Text)
    display_name = Column(Text)
    description = Column(Text)
    used_by = Column(Text)
    created_date = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_date = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    def __repr__(self):
        return f"<KnowledgeBase(id={self.id}, display_name={self.display_name})>"


class KnowledgeBaseFile(Base):
    __tablename__ = 'knowledgebase_files'

    id = Column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    
    # File information
    file_name = Column(String)
    s3_url = Column(String)
    bucket_name = Column(String)
    key = Column(String)
    s3_etag = Column(String)
    s3_version_id = Column(String)
    s3_last_modified = Column(DateTime(timezone=True))
    content_hash = Column(String(64))
    
    # Processing status
    status = Column(String, default='pending')
    
    # Timestamps
    upload_date = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    
    # Metadata
    content_type = Column(String)
    file_size = Column(Integer)
    page_count = Column(Integer)
    chunk_count = Column(Integer)
    document_title = Column(String(512))
    source_url = Column(String(2048))
    source_authority = Column(String(255))
    document_type = Column(String(100))
    jurisdiction = Column(String(100))
    practice_area = Column(String(100))
    published_at = Column(DateTime(timezone=True))
    effective_from = Column(DateTime(timezone=True))
    effective_to = Column(DateTime(timezone=True))
    is_current = Column(Boolean, default=True)
    superseded_by = Column(String)
    parser_version = Column(String(50))
    chunking_version = Column(String(50))
    
    # Foreign key relationship with KnowledgeBase
    knowledgebase_id = Column(Integer, ForeignKey('knowledge_base.id', ondelete='CASCADE'))
    
    # Vector store metadata
    vector_store_id = Column(String)
    embedding_model = Column(String)
    embedding_namespace = Column(String(100))

    def __repr__(self):
        return f"<KnowledgeBaseFile(id={self.id}, file_name={self.file_name}, status={self.status})>"


class KnowledgeBaseChunk(Base):
    __tablename__ = 'knowledgebase_chunks'

    id = Column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id = Column(PGUUID(as_uuid=True), ForeignKey('knowledgebase_files.id', ondelete='CASCADE'), nullable=False)
    vector_id = Column(String, unique=True, nullable=False)
    chunk_index = Column(Integer, nullable=False)
    previous_chunk_index = Column(Integer)
    next_chunk_index = Column(Integer)

    # Chunk content and location
    chunk_text = Column(Text, nullable=False)
    document_title = Column(String(512))
    section_heading = Column(String(512))
    page_number = Column(Integer)
    section_index = Column(Integer)
    section_chunk_index = Column(Integer)
    contains_table = Column(Boolean, default=False)
    table_count = Column(Integer, default=0)

    # Source metadata copied from the source document for fast citation display
    s3_url = Column(String)
    s3_key = Column(String)
    source_url = Column(String(2048))
    source_authority = Column(String(255))
    document_type = Column(String(100))
    jurisdiction = Column(String(100))
    practice_area = Column(String(100))
    published_at = Column(DateTime(timezone=True))
    effective_from = Column(DateTime(timezone=True))
    effective_to = Column(DateTime(timezone=True))
    is_current = Column(Boolean, default=True)

    # Processing metadata
    content_hash = Column(String(64))
    parser_version = Column(String(50))
    chunking_version = Column(String(50))
    embedding_model = Column(String(255))
    embedding_namespace = Column(String(100))
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    document = relationship("KnowledgeBaseFile", backref="chunks")

    def __repr__(self):
        return f"<KnowledgeBaseChunk(id={self.id}, vector_id={self.vector_id}, document_id={self.document_id})>"


class ScrapedUrl(Base):
    __tablename__ = 'scraped_urls'

    id = Column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    url = Column(String(2048), nullable=False)
    title = Column(String(512))
    description = Column(Text)
    content = Column(Text)
    status = Column(String(50), default='pending')  # pending, processed, failed
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    error_message = Column(Text)
    vector_store_id = Column(String(255))
    embedding_model = Column(String(255))
    chunk_count = Column(Integer)

    def __repr__(self):
        return f"<ScrapedUrl(id={self.id}, url={self.url}, status={self.status})>"


class PasswordResetToken(Base):
    __tablename__ = 'password_reset_tokens'

    id = Column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(PGUUID(as_uuid=True), ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    token_hash = Column(String, nullable=False)  # Store hash of the token/OTP for security
    otp_code = Column(String(6), nullable=True)  # Store the 6-digit OTP code
    expires_at = Column(DateTime(timezone=True), nullable=False)
    used = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    used_at = Column(DateTime(timezone=True), nullable=True)

    def __repr__(self):
        return f"<PasswordResetToken(id={self.id}, user_id={self.user_id}, expires_at={self.expires_at})>"


class EmailVerificationToken(Base):
    __tablename__ = 'email_verification_tokens'

    id = Column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email = Column(String, nullable=False)  # Email to be verified
    token_hash = Column(String, nullable=False)  # Store hash of the token/OTP for security
    otp_code = Column(String(6), nullable=True)  # Legacy column; new records never populate it
    attempts = Column(Integer, nullable=False, default=0, server_default="0")
    expires_at = Column(DateTime, nullable=False)
    used = Column(Boolean, default=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    used_at = Column(DateTime, nullable=True)
    # Store user registration data temporarily until verification
    registration_data = Column(JSON, nullable=True)  # JSON field to store user registration data

    def __repr__(self):
        return f"<EmailVerificationToken(id={self.id}, email={self.email}, expires_at={self.expires_at})>"

class RegistrationReminderStatus(Base):
    __tablename__ = 'registration_reminder_status'

    email = Column(String, primary_key=True)
    first_name = Column(String, nullable=True)
    professional_type = Column(String, nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    reminder_24h_sent_at = Column(DateTime(timezone=True), nullable=True)
    reminder_72h_sent_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    def __repr__(self):
        return f"<RegistrationReminderStatus(email={self.email}, 24h_sent={self.reminder_24h_sent_at is not None}, 72h_sent={self.reminder_72h_sent_at is not None})>"

class BetaFeedback(Base):
    __tablename__ = 'beta_feedback'

    id = Column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String, nullable=False)
    email = Column(String, nullable=False)
    phone = Column(String, nullable=True)
    comment = Column(Text, nullable=False)
    photo_path = Column(String, nullable=True)  # Path to uploaded photo if provided
    photo_filename = Column(String, nullable=True)  # Original filename
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    def __repr__(self):
        return f"<BetaFeedback(id={self.id}, name={self.name}, email={self.email})>"


class ChatResponseFeedback(Base):
    __tablename__ = 'chat_response_feedback'

    id = Column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(PGUUID(as_uuid=True), ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    conversation_id = Column(String, nullable=False)  # From Redis conversation
    message_index = Column(Integer, nullable=False)  # Position of the response in conversation
    response_content = Column(Text, nullable=False)  # Store the actual response that was rated
    user_question = Column(Text, nullable=False)  # Store the user question that led to this response
    feedback_type = Column(String, nullable=False)  # thumbs_up or thumbs_down
    thumbs_down_reason = Column(String, nullable=True)  # Reason if thumbs_down
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    def __repr__(self):
        return f"<ChatResponseFeedback(id={self.id}, user_id={self.user_id}, feedback_type={self.feedback_type})>"


class ConsultationPricing(Base):
    """Store consultation types and their pricing"""
    __tablename__ = 'consultation_pricing'

    id = Column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    consultation_type = Column(String, nullable=False)  # Using String for ConsultationType enum
    consultation_category = Column(String, nullable=True)  # Using String for ConsultationCategory enum
    duration_minutes = Column(Integer, nullable=True)  # Duration for single consultations
    duration_days = Column(Integer, nullable=True)     # Duration for ongoing support
    price_gbp = Column(String, nullable=True)          # Updated pricing format
    description = Column(Text, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    def __repr__(self):
        return f"<ConsultationPricing(type={self.consultation_type}, category={self.consultation_category})>"


class ReferralRequest(Base):
    """Store referral requests from users"""
    __tablename__ = 'referral_requests'

    id = Column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(PGUUID(as_uuid=True), ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    conversation_id = Column(String, nullable=False)  # Link to chat conversation
    
    # Legal issue details
    legal_area = Column(String, nullable=False)  # Immigration law, etc.
    issue_summary = Column(Text, nullable=False)  # AI-generated summary of the issue
    
    # Consultation preferences - updated structure
    consultation_category = Column(String, nullable=False)  # Single, Ongoing Support, Casework, Not Sure
    consultation_type = Column(String, nullable=False)  # Specific type (5_min, 3_day, etc.)
    budget_range = Column(String, nullable=True)  # Updated budget ranges
    ongoing_budget_gbp = Column(Integer, nullable=True)  # Exact ongoing support budget selected by the user

    # Casework-only fields. Populated when consultation_category == 'casework'.
    # casework_service_type stores the CaseworkServiceType value the user picked
    # from the Service & Price dropdown; casework_budget_gbp stores the exact £
    # figure the user committed to (always >= the service's published minimum,
    # enforced both in the chat referral flow and in /search/request-service).
    casework_service_type = Column(String, nullable=True)
    casework_budget_gbp = Column(Integer, nullable=True)

    # Location and timing - enhanced with inference
    user_location = Column(String, nullable=True)  # City/region
    latitude = Column(String, nullable=True)  # For geo-matching
    longitude = Column(String, nullable=True)  # For geo-matching
    location_inferred = Column(Boolean, default=False)  # Whether location was inferred
    urgency_level = Column(String, nullable=False, default='normal')  # UrgencyLevel enum
    urgency_inferred = Column(Boolean, default=False)  # Whether urgency was AI-inferred
    urgency_keywords = Column(ARRAY(String), nullable=True)  # Keywords that triggered urgency inference
    
    # Status tracking
    status = Column(String, nullable=False, default='pending')  # ReferralStatus enum
    matched_lawyer_id = Column(PGUUID(as_uuid=True), ForeignKey('users.id'), nullable=True)
    
    # Chat transcript and AI analysis
    chat_history = Column(JSON, nullable=True)  # Store the relevant chat history
    confirmation_summary = Column(Text, nullable=True)  # Pre-handoff confirmation text
    user_confirmed = Column(Boolean, default=False)     # Whether user confirmed the summary
    additional_info = Column(Text, nullable=True)       # Any additional info user provided
    
    # File attachments
    voice_note_url = Column(String, nullable=True)      # S3 URL for voice note
    pdf_attachment_url = Column(String, nullable=True)  # S3 URL for PDF attachment
    docx_attachment_url = Column(String, nullable=True) # S3 URL for DOCX attachment
    image_attachment_url = Column(String, nullable=True) # S3 URL for image attachment
    attachment_metadata = Column(JSON, nullable=True)   # File metadata (name, size, type, etc.)
    
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    def __repr__(self):
        return f"<ReferralRequest(id={self.id}, user_id={self.user_id}, category={self.consultation_category}, status={self.status})>"


class ReferralSession(Base):
    """Store session between lawyer and client"""
    __tablename__ = 'referral_sessions'

    id = Column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    referral_request_id = Column(PGUUID(as_uuid=True), ForeignKey('referral_requests.id', ondelete='CASCADE'), nullable=False)
    lawyer_id = Column(PGUUID(as_uuid=True), ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    user_id = Column(PGUUID(as_uuid=True), ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    
    session_status = Column(String, nullable=False, default='pending')  # pending, active, completed, cancelled, missed
    payment_id = Column(
        PGUUID(as_uuid=True),
        ForeignKey('consultation_payments.id', name='fk_referral_sessions_payment_id', use_alter=True),
        nullable=True
    )
    start_time = Column(DateTime(timezone=True), nullable=True)
    end_time = Column(DateTime(timezone=True), nullable=True)
    timer_started_at = Column(DateTime(timezone=True), nullable=True)
    timer_ended_at = Column(DateTime(timezone=True), nullable=True)
    actual_duration_seconds = Column(Integer, nullable=True)
    timer_paused_at = Column(DateTime(timezone=True), nullable=True)
    paused_remaining_seconds = Column(Integer, nullable=True)
    current_communication_mode = Column(String(10), nullable=True)  # chat, voice, video
    timer_paused_reason = Column(String(20), nullable=True)  # pending_request, reconnecting, ending_questions
    end_requested_at = Column(DateTime(timezone=True), nullable=True)
    end_requested_by = Column(PGUUID(as_uuid=True), ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
    end_request_status = Column(String(40), nullable=True)
    end_conclusion_answers = Column(JSON, nullable=True)
    end_conclusion_completed_at = Column(DateTime(timezone=True), nullable=True)

    # Multi-day ongoing support fields (NULL for single-session consultations)
    days_total = Column(Integer, nullable=True)           # 3 or 5 for ongoing types
    days_completed = Column(Integer, nullable=False, default=0)
    current_day_timer_started_at = Column(DateTime(timezone=True), nullable=True)
    current_day_timer_ended_at = Column(DateTime(timezone=True), nullable=True)

    # Session tracking
    messages_exchanged = Column(Integer, default=0)
    notes = Column(Text, nullable=True)  # Lawyer's notes about the session
    missed_notification_sent_at = Column(DateTime(timezone=True), nullable=True)
    
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    def __repr__(self):
        return f"<ReferralSession(id={self.id}, lawyer_id={self.lawyer_id}, user_id={self.user_id}, status={self.session_status})>"


class CallType(enum.Enum):
    VOICE = "voice"
    VIDEO = "video"


class RequestedBy(enum.Enum):
    USER = "user"
    LAWYER = "lawyer"


class CallStatus(enum.Enum):
    REQUESTED = "requested"
    RINGING = "ringing"
    ACCEPTED = "accepted"
    CONNECTED = "connected"
    ENDED = "ended"
    DECLINED = "declined"
    CANCELLED = "cancelled"
    MISSED = "missed"
    FAILED = "failed"


class Call(Base):
    __tablename__ = 'calls'

    id = Column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    referral_session_id = Column(PGUUID(as_uuid=True), ForeignKey('referral_sessions.id', ondelete='CASCADE'), nullable=False)
    user_id = Column(PGUUID(as_uuid=True), ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    lawyer_id = Column(PGUUID(as_uuid=True), ForeignKey('users.id', ondelete='CASCADE'), nullable=False)

    call_type = Column(String, nullable=False)  # voice, video
    requested_by = Column(String, nullable=False)  # user, lawyer
    status = Column(String, nullable=False, default='requested')  # requested, ringing, accepted, connected, ended, declined, cancelled, missed, failed

    agora_channel_name = Column(String, unique=True, nullable=False)

    requested_at = Column(DateTime(timezone=True), nullable=True)
    accepted_at = Column(DateTime(timezone=True), nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    ended_at = Column(DateTime(timezone=True), nullable=True)
    failure_reason = Column(Text, nullable=True)
    disconnected_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    def __repr__(self):
        return f"<Call(id={self.id}, type={self.call_type}, status={self.status})>"



class ConsultationSurvey(Base):
    """Private post-consultation survey submitted by a session participant."""
    __tablename__ = 'consultation_surveys'
    __table_args__ = (
        UniqueConstraint('session_id', 'user_id', name='uq_consultation_survey_session_user'),
    )

    id = Column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id = Column(PGUUID(as_uuid=True), ForeignKey('referral_sessions.id', ondelete='CASCADE'), nullable=False)
    referral_request_id = Column(PGUUID(as_uuid=True), ForeignKey('referral_requests.id', ondelete='CASCADE'), nullable=False)
    user_id = Column(PGUUID(as_uuid=True), ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    lawyer_id = Column(PGUUID(as_uuid=True), ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    reviewer_role = Column(String(20), nullable=False)  # client or lawyer

    overall_rating = Column(Integer, nullable=False)
    lawyer_rating = Column(Integer, nullable=True)
    platform_rating = Column(Integer, nullable=True)
    was_helpful = Column(Boolean, nullable=True)
    would_recommend = Column(Boolean, nullable=True)
    comment = Column(Text, nullable=True)
    issue_tags = Column(JSON, nullable=True)

    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    def __repr__(self):
        return f"<ConsultationSurvey(id={self.id}, session_id={self.session_id}, user_id={self.user_id})>"


class ConsultationPayment(Base):
    """Store payment and payout information for consultation marketplace sessions"""
    __tablename__ = 'consultation_payments'

    id = Column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id = Column(PGUUID(as_uuid=True), ForeignKey('referral_sessions.id', ondelete='CASCADE'), nullable=False, unique=True)
    referral_request_id = Column(PGUUID(as_uuid=True), ForeignKey('referral_requests.id', ondelete='CASCADE'), nullable=True)
    lawyer_id = Column(PGUUID(as_uuid=True), ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    user_id = Column(PGUUID(as_uuid=True), ForeignKey('users.id', ondelete='CASCADE'), nullable=False)

    stripe_payment_intent_id = Column(String(100), nullable=True, unique=True)
    stripe_transfer_id = Column(String(100), nullable=True)
    stripe_customer_id = Column(String(100), nullable=True)
    stripe_charge_id = Column(String(100), nullable=True)
    net_amount_pence = Column(Integer, nullable=True)
    vat_rate = Column(Numeric(5, 2), nullable=True)
    vat_amount_pence = Column(Integer, nullable=True)
    gross_amount_pence = Column(Integer, nullable=True)
    total_amount_pence = Column(Integer, nullable=False)
    pricing_type = Column(String(20), nullable=False)
    commission_rate = Column(Numeric(5, 2), nullable=False)
    commission_amount_pence = Column(Integer, nullable=False)
    lawyer_payout_amount_pence = Column(Integer, nullable=False)
    payment_status = Column(String(20), nullable=False, default=ConsultationPaymentStatus.PENDING.value)

    paid_at = Column(DateTime, nullable=True)
    transferred_at = Column(DateTime, nullable=True)
    refunded_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    def __repr__(self):
        return (
            f"<ConsultationPayment(id={self.id}, session_id={self.session_id}, "
            f"status={self.payment_status}, amount_pence={self.total_amount_pence})>"
        )


class ConsultationExtensionRequest(Base):
    """Persisted approval state for one consultation timer extension."""
    __tablename__ = 'consultation_extension_requests'

    id = Column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id = Column(
        PGUUID(as_uuid=True),
        ForeignKey('referral_sessions.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
    )
    user_id = Column(PGUUID(as_uuid=True), ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    lawyer_id = Column(PGUUID(as_uuid=True), ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    day_number = Column(Integer, nullable=True)
    requested_minutes = Column(Integer, nullable=True)
    proposed_minutes = Column(Integer, nullable=True)
    agreed_minutes = Column(Integer, nullable=True)
    status = Column(String(30), nullable=False, default=ConsultationExtensionStatus.DRAFT.value, index=True)
    paused_remaining_seconds = Column(Integer, nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)
    submitted_at = Column(DateTime(timezone=True), nullable=True)
    decided_at = Column(DateTime(timezone=True), nullable=True)
    paid_at = Column(DateTime(timezone=True), nullable=True)
    applied_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class ConsultationExtensionPayment(Base):
    """Stripe charge and payout snapshot for an approved extension."""
    __tablename__ = 'consultation_extension_payments'

    id = Column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    extension_request_id = Column(
        PGUUID(as_uuid=True),
        ForeignKey('consultation_extension_requests.id', ondelete='CASCADE'),
        nullable=False,
        unique=True,
        index=True,
    )
    session_id = Column(
        PGUUID(as_uuid=True),
        ForeignKey('referral_sessions.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
    )
    stripe_payment_intent_id = Column(String(100), nullable=True, unique=True)
    stripe_customer_id = Column(String(100), nullable=True)
    stripe_charge_id = Column(String(100), nullable=True)
    stripe_transfer_id = Column(String(100), nullable=True)
    net_amount_pence = Column(Integer, nullable=False)
    vat_rate = Column(Numeric(5, 2), nullable=False)
    vat_amount_pence = Column(Integer, nullable=False)
    gross_amount_pence = Column(Integer, nullable=False)
    commission_rate = Column(Numeric(5, 2), nullable=False)
    commission_amount_pence = Column(Integer, nullable=False)
    lawyer_payout_amount_pence = Column(Integer, nullable=False)
    payment_status = Column(String(20), nullable=False, default=ConsultationPaymentStatus.PENDING.value, index=True)
    paid_at = Column(DateTime(timezone=True), nullable=True)
    transferred_at = Column(DateTime(timezone=True), nullable=True)
    refunded_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )



class LawyerRecommendation(Base):
    """Store recommended lawyers for a referral request"""
    __tablename__ = 'lawyer_recommendations'

    id = Column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    referral_request_id = Column(PGUUID(as_uuid=True), ForeignKey('referral_requests.id', ondelete='CASCADE'), nullable=False)
    lawyer_id = Column(PGUUID(as_uuid=True), ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    
    # Recommendation details
    is_best_suited = Column(Boolean, default=False)  # Mark one lawyer as best suited
    match_score = Column(Integer, nullable=False)  # Matching score (0-100)
    match_reasons = Column(ARRAY(String), nullable=True)  # Why this lawyer was recommended
    
    # Display order
    recommendation_rank = Column(Integer, nullable=False)  # 1, 2, 3 for ordering
    
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    def __repr__(self):
        return f"<LawyerRecommendation(referral_id={self.referral_request_id}, lawyer_id={self.lawyer_id}, rank={self.recommendation_rank}, best_suited={self.is_best_suited})>"


class LawyerInbox(Base):
    """Store referral requests in lawyer's inbox"""
    __tablename__ = 'lawyer_inbox'

    id = Column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    lawyer_id = Column(PGUUID(as_uuid=True), ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    referral_request_id = Column(PGUUID(as_uuid=True), ForeignKey('referral_requests.id', ondelete='CASCADE'), nullable=False)
    
    # Inbox status
    status = Column(String, nullable=False, default='pending')  # pending, read, accepted, rejected
    read_at = Column(DateTime, nullable=True)
    responded_at = Column(DateTime, nullable=True)
    response_message = Column(Text, nullable=True)  # Lawyer's response message
    
    # Priority and urgency from the referral
    priority_score = Column(Integer, nullable=False, default=50)  # 1-100, higher = more urgent
    
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    def __repr__(self):
        return f"<LawyerInbox(id={self.id}, lawyer_id={self.lawyer_id}, referral_id={self.referral_request_id}, status={self.status})>"


class LawyerAvailability(Base):
    """Track lawyer availability for consultations"""
    __tablename__ = 'lawyer_availability'

    id = Column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    lawyer_id = Column(PGUUID(as_uuid=True), ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    
    # Availability settings - updated for new consultation types
    is_available = Column(Boolean, default=True)
    consultation_categories = Column(ARRAY(String), nullable=True)  # Which categories they offer
    consultation_types = Column(ARRAY(String), nullable=True)  # Specific types they offer
    max_daily_consultations = Column(Integer, default=10)
    current_daily_consultations = Column(Integer, default=0)
    
    # Pricing for different consultation types
    single_consultation_pricing = Column(JSON, nullable=True)  # Pricing for single consultations
    ongoing_support_pricing = Column(JSON, nullable=True)     # Pricing for ongoing support
    casework_pricing = Column(JSON, nullable=True)            # Pricing for casework
    
    # Preferences
    preferred_legal_areas = Column(ARRAY(String), nullable=True)
    preferred_urgency_levels = Column(ARRAY(String), nullable=True)
    preferred_budget_ranges = Column(ARRAY(String), nullable=True)  # Which budget ranges they accept
    
    # Location preferences
    service_radius_km = Column(Integer, default=50)  # How far they're willing to serve
    
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    def __repr__(self):
        return f"<LawyerAvailability(lawyer_id={self.lawyer_id}, is_available={self.is_available})>"


class ProfessionalAvailabilityRule(Base):
    """Recurring weekly availability windows for a professional."""
    __tablename__ = 'professional_availability_rules'

    id = Column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    professional_id = Column(PGUUID(as_uuid=True), ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    weekday = Column(Integer, nullable=False)  # Monday=0, Sunday=6 in the rule timezone
    start_minute = Column(Integer, nullable=False)  # minutes after local midnight
    end_minute = Column(Integer, nullable=False)
    timezone = Column(String(100), nullable=False, default='Europe/London')
    consultation_types = Column(ARRAY(String), nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    def __repr__(self):
        return f"<ProfessionalAvailabilityRule(professional_id={self.professional_id}, weekday={self.weekday})>"


class ProfessionalAvailabilityException(Base):
    """One-off available or unavailable blocks in UTC."""
    __tablename__ = 'professional_availability_exceptions'

    id = Column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    professional_id = Column(PGUUID(as_uuid=True), ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    start_at = Column(DateTime(timezone=True), nullable=False, index=True)
    end_at = Column(DateTime(timezone=True), nullable=False, index=True)
    timezone = Column(String(100), nullable=False, default='Europe/London')
    is_available = Column(Boolean, nullable=False, default=False)
    consultation_types = Column(ARRAY(String), nullable=True)
    reason = Column(String(255), nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    def __repr__(self):
        return f"<ProfessionalAvailabilityException(professional_id={self.professional_id}, available={self.is_available})>"


class ConsultationBookingRequest(Base):
    """LawNeeds-owned booking request linked to a referral request."""
    __tablename__ = 'consultation_booking_requests'

    id = Column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(PGUUID(as_uuid=True), ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    professional_id = Column(PGUUID(as_uuid=True), ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    referral_request_id = Column(PGUUID(as_uuid=True), ForeignKey('referral_requests.id', ondelete='CASCADE'), nullable=True, unique=True, index=True)
    consultation_category = Column(String(50), nullable=False)
    consultation_type = Column(String(50), nullable=False)
    requested_start_at = Column(DateTime(timezone=True), nullable=False, index=True)
    requested_end_at = Column(DateTime(timezone=True), nullable=False, index=True)
    timezone = Column(String(100), nullable=False, default='Europe/London')
    status = Column(String(30), nullable=False, default=ConsultationBookingStatus.PENDING.value, index=True)
    reminder_24h_sent_at = Column(DateTime(timezone=True), nullable=True)   # <-- ADD THIS LINE
    reminder_72h_sent_at = Column(DateTime(timezone=True), nullable=True)   # <-- ADD THIS LINE
    accepted_at = Column(DateTime(timezone=True), nullable=True)
    cancelled_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    def __repr__(self):
        return f"<ConsultationBookingRequest(id={self.id}, professional_id={self.professional_id}, status={self.status})>"


class ProfessionalPresence(Base):
    """Professional live availability signals; effective status is computed server-side."""
    __tablename__ = 'professional_presence'
    __table_args__ = (
        UniqueConstraint('professional_id', name='uq_professional_presence_professional_id'),
    )

    id = Column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    professional_id = Column(PGUUID(as_uuid=True), ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    last_heartbeat_at = Column(DateTime(timezone=True), nullable=True)
    manual_status_override = Column(String(20), nullable=True)  # away, offline, or None
    accepting_consultations = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    def __repr__(self):
        return f"<ProfessionalPresence(professional_id={self.professional_id}, override={self.manual_status_override})>"


class InboxMessage(Base):
    """Store messages exchanged between users and lawyers in inbox conversations"""
    __tablename__ = 'inbox_messages'
    
    id = Column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    referral_request_id = Column(PGUUID(as_uuid=True), ForeignKey('referral_requests.id', ondelete='CASCADE'), nullable=False)
    sender_id = Column(PGUUID(as_uuid=True), ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    
    # Message details
    sender_type = Column(String, nullable=False)  # 'user' or 'lawyer'
    message_content = Column(Text, nullable=True)  # Text content
    message_type = Column(String, nullable=False, default='text')  # 'text', 'file', 'voice_note'
    
    # File attachments
    file_url = Column(String, nullable=True)  # S3 URL for files
    file_name = Column(String, nullable=True)  # Original filename
    file_type = Column(String, nullable=True)  # MIME type
    file_size = Column(Integer, nullable=True)  # File size in bytes
    file_attachments = Column(JSON, nullable=True)  # list of {url, file_name, file_type, file_size, category}
    
    # Message status
    is_read = Column(Boolean, default=False)
    read_at = Column(DateTime, nullable=True)
    
    # Metadata
    reply_to_message_id = Column(PGUUID(as_uuid=True), ForeignKey('inbox_messages.id'), nullable=True)  # For threaded replies
    
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    def __repr__(self):
        return f"<InboxMessage(id={self.id}, referral_id={self.referral_request_id}, sender_type={self.sender_type}, type={self.message_type})>"


class Conversation(Base):
    """Store user conversations persistently"""
    __tablename__ = 'conversations'
    
    id = Column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(PGUUID(as_uuid=True), ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    title = Column(String, nullable=True)  # AI-generated conversation title
    
    # Conversation metadata
    first_message = Column(Text, nullable=True)  # Store first user message for context
    last_activity = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    message_count = Column(Integer, default=0)
    
    # Status and categorization
    is_active = Column(Boolean, default=True)
    conversation_type = Column(String, default='general')  # 'general', 'referral', 'legal_advice'
    language = Column(String, default='en')
    
    # Timestamps
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    
    # Relationship to messages
    messages = relationship("ChatMessage", back_populates="conversation", cascade="all, delete-orphan")
    
    def __repr__(self):
        return f"<Conversation(id={self.id}, user_id={self.user_id}, title={self.title})>"


class ChatMessage(Base):
    """Store individual chat messages persistently"""
    __tablename__ = 'chat_messages'
    
    id = Column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conversation_id = Column(PGUUID(as_uuid=True), ForeignKey('conversations.id', ondelete='CASCADE'), nullable=False)
    user_id = Column(PGUUID(as_uuid=True), ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    
    # Message content
    role = Column(String, nullable=False)  # 'user' or 'assistant'
    content = Column(Text, nullable=False)
    message_index = Column(Integer, nullable=False)  # Order within conversation
    
    # Message metadata
    language = Column(String, nullable=True)
    source_type = Column(String, default='text')  # 'text', 'voice', 'image'

    # Image attachment (for chatbot document analysis)
    image_url = Column(String, nullable=True)
    image_s3_key = Column(String, nullable=True)
    image_analysis_type = Column(String, nullable=True)  # 'legal_document', 'general'

    # AI response metadata (for assistant messages)
    response_metadata = Column(JSON, nullable=True)  # Store validation results, source docs, etc.
    
    # Feedback and quality
    feedback_type = Column(String, nullable=True)  # 'thumbs_up', 'thumbs_down'
    feedback_reason = Column(String, nullable=True)
    
    # Timestamps
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    
    # Relationships
    conversation = relationship("Conversation", back_populates="messages")
    
    def __repr__(self):
        return f"<ChatMessage(id={self.id}, conversation_id={self.conversation_id}, role={self.role})>"


class ChatResponseSource(Base):
    __tablename__ = 'chat_response_sources'

    id = Column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conversation_id = Column(PGUUID(as_uuid=True), ForeignKey('conversations.id', ondelete='CASCADE'), nullable=False)
    message_id = Column(PGUUID(as_uuid=True), ForeignKey('chat_messages.id', ondelete='CASCADE'), nullable=False)
    user_id = Column(PGUUID(as_uuid=True), ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    document_id = Column(PGUUID(as_uuid=True), ForeignKey('knowledgebase_files.id', ondelete='SET NULL'), nullable=True)
    chunk_id = Column(PGUUID(as_uuid=True), ForeignKey('knowledgebase_chunks.id', ondelete='SET NULL'), nullable=True)
    vector_id = Column(String)

    similarity_score = Column(Float)
    rerank_score = Column(Float)

    source_title = Column(String(512))
    source_section = Column(String(512))
    source_page = Column(Integer)
    source_url = Column(String(2048))
    source_authority = Column(String(255))
    document_type = Column(String(100))
    jurisdiction = Column(String(100))
    practice_area = Column(String(100))
    published_at = Column(DateTime(timezone=True))
    effective_from = Column(DateTime(timezone=True))
    effective_to = Column(DateTime(timezone=True))
    is_current = Column(Boolean, default=True)
    source_metadata = Column(JSON)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    conversation = relationship("Conversation")
    message = relationship("ChatMessage", backref="response_sources")
    document = relationship("KnowledgeBaseFile")
    chunk = relationship("KnowledgeBaseChunk")

    def __repr__(self):
        return f"<ChatResponseSource(id={self.id}, message_id={self.message_id}, vector_id={self.vector_id})>"


class LawyerReview(Base):
    """Store reviews for lawyers"""
    __tablename__ = 'lawyer_reviews'
    
    id = Column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    reviewer_id = Column(PGUUID(as_uuid=True), ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    lawyer_id = Column(PGUUID(as_uuid=True), ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    
    rating = Column(Integer, nullable=False)  # 1-5 stars
    review_text = Column(Text, nullable=True)
    reviewer_name = Column(String(100), nullable=False)
    
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    
    def __repr__(self):
        return f"<LawyerReview(id={self.id}, lawyer_id={self.lawyer_id}, rating={self.rating})>"


class LawyerReviewStats(Base):
    """Store aggregated review statistics for lawyers"""
    __tablename__ = 'lawyer_review_stats'
    
    lawyer_id = Column(PGUUID(as_uuid=True), ForeignKey('users.id', ondelete='CASCADE'), primary_key=True)
    total_reviews = Column(Integer, default=0)
    average_rating = Column(Numeric(3, 2), default=0)
    last_updated = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    
    def __repr__(self):
        return f"<LawyerReviewStats(lawyer_id={self.lawyer_id}, avg_rating={self.average_rating})>"


class DeviceToken(Base):
    """Store device tokens for push notifications"""
    __tablename__ = 'device_tokens'
    
    id = Column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(PGUUID(as_uuid=True), ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    device_token = Column(String(255), nullable=False, unique=True)
    device_type = Column(String(20), nullable=False)  # 'ios' or 'android'
    device_name = Column(String(100), nullable=True)  # e.g., "iPhone 13", "Samsung Galaxy S21"
    is_active = Column(Boolean, default=True)
    app_version = Column(String(20), nullable=True)  # Last-reported app version from this device (e.g. "2.4.1"), used for app_update_available checks
    app_update_notified_at = Column(DateTime(timezone=True), nullable=True)  # Last time this device was sent an app_update_available notification for its current version gap
    
    # Timestamps
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    last_used_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    
    def __repr__(self):
        return f"<DeviceToken(id={self.id}, user_id={self.user_id}, device_type={self.device_type}, is_active={self.is_active})>"


# ============================================
# NOTIFICATION HISTORY MODEL
# ============================================

class Notification(Base):
    """Persisted in-app notification record for both users and lawyers."""
    __tablename__ = 'notifications'

    id = Column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    recipient_id = Column(
        PGUUID(as_uuid=True),
        ForeignKey('users.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
    )
    title = Column(String(255), nullable=False)
    body = Column(Text, nullable=False)
    notification_type = Column(String(50), nullable=False)
    data = Column(JSON, nullable=True)
    is_read = Column(Boolean, nullable=False, default=False)
    read_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    def __repr__(self):
        return (
            f"<Notification(id={self.id}, recipient={self.recipient_id}, "
            f"type={self.notification_type}, is_read={self.is_read})>"
        )


# ============================================
# SUBSCRIPTION & PRICING SYSTEM MODELS
# ============================================

class SubscriptionPlan(Base):
    """
    Reference table for available subscription plans.
    Based on: LAWNEEDS MINUTES-BASED SYSTEM specification
    """
    __tablename__ = 'subscription_plans'
    
    id = Column(String(50), primary_key=True)  # e.g., 'standard_weekly'
    name = Column(String(100), nullable=False)  # e.g., 'Standard Weekly'
    tier = Column(String(20), nullable=False)  # 'freemium', 'standard', 'premium'
    billing_period = Column(String(20), nullable=True)  # 'weekly', 'monthly', 'annual', None for freemium
    price_gbp = Column(Numeric(10, 2), nullable=False, default=0)
    minutes_included = Column(Integer, nullable=False, default=0)
    
    # Service limits (None = unlimited)
    voice_notes_limit = Column(Integer, nullable=True)  # Monthly limit
    uploads_limit = Column(Integer, nullable=True)  # Monthly limit
    docgen_limit = Column(Integer, nullable=True)  # Monthly limit
    
    # File size caps
    max_image_size_mb = Column(Integer, default=25)
    max_doc_pages = Column(Integer, default=8)
    max_docgen_pages = Column(Integer, nullable=True)  # Page limit for doc generation (freemium only)
    
    # Features
    unlimited_ai_queries = Column(Boolean, default=False)
    rollover_enabled = Column(Boolean, default=False)
    priority_support = Column(Boolean, default=False)
    early_access = Column(Boolean, default=False)
    
    # Annual bonus
    free_consultation_minutes = Column(Integer, nullable=True)  # 10 for Standard Annual, 20 for Premium Annual
    
    # Stripe integration
    stripe_price_id = Column(String(100), nullable=True)
    stripe_product_id = Column(String(100), nullable=True)
    
    # Status
    is_active = Column(Boolean, default=True)
    display_order = Column(Integer, default=0)
    
    # Timestamps
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    
    def __repr__(self):
        return f"<SubscriptionPlan(id={self.id}, name={self.name}, tier={self.tier})>"


class UserSubscription(Base):
    """
    Track user subscription status.
    Each user has one active subscription (defaults to freemium).
    """
    __tablename__ = 'user_subscriptions'
    
    id = Column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(PGUUID(as_uuid=True), ForeignKey('users.id', ondelete='CASCADE'), nullable=False, unique=True)
    
    # Current plan
    plan_id = Column(String(50), ForeignKey('subscription_plans.id'), nullable=False, default='freemium')
    tier = Column(String(20), nullable=False, default='freemium')  # 'freemium', 'standard', 'premium'
    billing_period = Column(String(20), nullable=True)  # 'weekly', 'monthly', 'annual'
    
    # Subscription dates
    started_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    current_period_start = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    current_period_end = Column(DateTime, nullable=True)  # None for freemium
    cancelled_at = Column(DateTime, nullable=True)
    
    # Status
    status = Column(String(20), nullable=False, default='active')  # 'active', 'cancelled', 'expired', 'past_due', 'trialing'
    auto_renew = Column(Boolean, default=True)
    
    # Stripe integration
    stripe_customer_id = Column(String(100), nullable=True)
    stripe_subscription_id = Column(String(100), nullable=True)

    # RevenueCat integration
    revenuecat_app_user_id = Column(String(100), nullable=True)
    revenuecat_entitlement_id = Column(String(100), nullable=True)
    revenuecat_product_id = Column(String(100), nullable=True)
    revenuecat_original_transaction_id = Column(String(100), nullable=True)
    
    # Promotions tracking
    first_month_discount_used = Column(Boolean, default=False)
    is_first_month = Column(Boolean, default=True)
    
    # Pending changes (scheduled for period end)
    pending_downgrade_to = Column(String(50), nullable=True)  # Plan ID to downgrade to at period end
    
    # Timestamps
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    
    def __repr__(self):
        return f"<UserSubscription(id={self.id}, user_id={self.user_id}, plan={self.plan_id}, status={self.status})>"


class MinutesAccount(Base):
    """
    Track user minutes balance.
    Tracks total, used, rollover, and top-up minutes.
    """
    __tablename__ = 'minutes_accounts'
    
    id = Column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(PGUUID(as_uuid=True), ForeignKey('users.id', ondelete='CASCADE'), nullable=False, unique=True)
    
    # Minutes balance
    total_minutes = Column(Integer, default=0)  # Total minutes for current period (from plan)
    used_minutes = Column(Integer, default=0)  # Minutes used this period
    rollover_minutes = Column(Integer, default=0)  # Rolled over from previous period
    top_up_minutes = Column(Integer, default=0)  # Purchased top-ups (never expire)
    
    # Period tracking
    period_start = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    period_end = Column(DateTime, nullable=True)
    
    # Top-up tracking
    top_ups_this_month = Column(Integer, default=0)
    last_top_up_at = Column(DateTime, nullable=True)
    
    # Timestamps
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    
    @property
    def available_minutes(self) -> int:
        """Calculate available minutes including rollover and top-ups"""
        return max(0, self.total_minutes - self.used_minutes + self.rollover_minutes + self.top_up_minutes)
    
    @property
    def usage_percentage(self) -> float:
        """Calculate usage percentage (0-100)"""
        if self.total_minutes == 0:
            return 0.0
        return min(100.0, (self.used_minutes / self.total_minutes) * 100)
    
    def __repr__(self):
        return f"<MinutesAccount(user_id={self.user_id}, available={self.available_minutes})>"


class UsageTracking(Base):
    """
    Track monthly service usage per user.
    Separate counts for voice notes, uploads, and document generation.
    """
    __tablename__ = 'usage_tracking'
    
    id = Column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(PGUUID(as_uuid=True), ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    
    # Period (unique per user per month)
    month = Column(Integer, nullable=False)  # 1-12
    year = Column(Integer, nullable=False)
    
    # Service counts
    voice_note_count = Column(Integer, default=0)
    upload_count = Column(Integer, default=0)
    docgen_count = Column(Integer, default=0)
    query_count = Column(Integer, default=0)  # For analytics
    
    # Minutes used by service type
    voice_note_minutes_used = Column(Integer, default=0)
    upload_minutes_used = Column(Integer, default=0)
    docgen_minutes_used = Column(Integer, default=0)
    
    # Timestamps
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    
    # Unique constraint: one record per user per month
    __table_args__ = (
        # SQLAlchemy UniqueConstraint
        {'sqlite_autoincrement': True},
    )
    
    def __repr__(self):
        return f"<UsageTracking(user_id={self.user_id}, {self.year}-{self.month:02d})>"


class FreemiumLimits(Base):
    """
    Track freemium user limitations.
    Manages query limits, daily pass, ad bonuses, and trial allowances.
    """
    __tablename__ = 'freemium_limits'
    
    id = Column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(PGUUID(as_uuid=True), ForeignKey('users.id', ondelete='CASCADE'), nullable=False, unique=True)
    
    # Query limiting (6 per 6 hours)
    query_count_current_window = Column(Integer, default=0)
    window_start = Column(DateTime, nullable=True)
    window_end = Column(DateTime, nullable=True)
    
    # £5 Daily pass for unlimited queries
    daily_pass_active = Column(Boolean, default=False)
    daily_pass_expires_at = Column(DateTime, nullable=True)
    daily_pass_purchased_at = Column(DateTime, nullable=True)
    
    # Ad watch bonus (6 extra queries per ad)
    ad_bonus_queries = Column(Integer, default=0)
    last_ad_watched_at = Column(DateTime, nullable=True)
    ads_watched_today = Column(Integer, default=0)
    
    # Trial allowances (first month only)
    trial_voice_notes_used = Column(Integer, default=0)  # 2/month limit
    trial_uploads_used = Column(Integer, default=0)  # 2/month limit
    trial_docgen_used = Column(Integer, default=0)  # 2/month limit
    
    # Trial period
    trial_started_at = Column(DateTime, nullable=True)
    trial_expires_at = Column(DateTime, nullable=True)
    trial_expired = Column(Boolean, default=False)
    
    # Timestamps
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    
    @property
    def queries_remaining(self) -> int:
        """
        Calculate remaining queries in current window.
        
        If the window has expired, returns the base allowance (bonuses reset on new window).
        Otherwise, calculates: base_allowance + current_bonus - queries_used
        """
        from services.subscription_constants import FREEMIUM_QUERIES_PER_WINDOW
        from datetime import datetime, timezone
        
        now = datetime.now(timezone.utc)
        
        # If window has expired, user gets fresh base allowance (bonuses reset)
        if self.window_end:
            # Ensure window_end is timezone-aware for comparison
            window_end = self.window_end
            if window_end.tzinfo is None:
                # If naive, assume UTC
                window_end = window_end.replace(tzinfo=timezone.utc)
            if now > window_end:
                return FREEMIUM_QUERIES_PER_WINDOW
        
        # Total allowed in this window = base + bonuses earned this window
        total_allowed = FREEMIUM_QUERIES_PER_WINDOW + self.ad_bonus_queries
        # Remaining = total allowed - queries used
        remaining = total_allowed - self.query_count_current_window
        return max(0, remaining)
    
    def __repr__(self):
        return f"<FreemiumLimits(user_id={self.user_id}, queries_remaining={self.queries_remaining})>"


class UsageLog(Base):
    """
    Audit log for all minute-consuming actions.
    Provides complete history for analytics and debugging.
    """
    __tablename__ = 'usage_logs'
    
    id = Column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(PGUUID(as_uuid=True), ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    
    # Action details
    action_type = Column(String(30), nullable=False)  # 'voice_note', 'upload', 'docgen', 'query'
    action_description = Column(String(255), nullable=True)
    
    # Minutes tracking
    minutes_deducted = Column(Integer, default=0)
    minutes_before = Column(Integer, nullable=False)
    minutes_after = Column(Integer, nullable=False)
    
    # Context (for linking to other records)
    referral_id = Column(PGUUID(as_uuid=True), nullable=True)
    conversation_id = Column(String(100), nullable=True)
    
    # File details (if applicable)
    file_size_bytes = Column(Integer, nullable=True)
    file_name = Column(String(255), nullable=True)
    duration_seconds = Column(Integer, nullable=True)  # For voice notes
    page_count = Column(Integer, nullable=True)  # For doc generation
    
    # Status
    status = Column(String(20), default='success')  # 'success', 'blocked', 'failed'
    blocked_reason = Column(String(50), nullable=True)  # BlockedReason enum value
    error_message = Column(Text, nullable=True)
    
    # Timestamp
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)
    
    def __repr__(self):
        return f"<UsageLog(id={self.id}, user_id={self.user_id}, action={self.action_type}, status={self.status})>"


class TopUpTransaction(Base):
    """
    Record of top-up purchases.
    £1 = 1 minute, min 5 minutes (£5), max 30 minutes (£30).
    """
    __tablename__ = 'top_up_transactions'
    
    id = Column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(PGUUID(as_uuid=True), ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    
    # Transaction details
    minutes_purchased = Column(Integer, nullable=False)
    amount_gbp = Column(Numeric(10, 2), nullable=False)
    
    # Stripe payment
    stripe_payment_intent_id = Column(String(100), nullable=True)
    stripe_charge_id = Column(String(100), nullable=True)
    payment_method = Column(String(50), nullable=True)  # 'card', 'apple_pay', etc.

    # RevenueCat purchase
    revenuecat_purchase_id = Column(String(100), nullable=True)
    revenuecat_product_id = Column(String(100), nullable=True)
    
    # Status
    payment_status = Column(String(20), default='pending')  # 'pending', 'succeeded', 'failed', 'refunded'
    
    # Applied to account
    applied_to_account = Column(Boolean, default=False)
    applied_at = Column(DateTime, nullable=True)
    
    # Error tracking
    error_message = Column(Text, nullable=True)
    
    # Timestamps
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    
    def __repr__(self):
        return f"<TopUpTransaction(id={self.id}, user_id={self.user_id}, minutes={self.minutes_purchased}, status={self.payment_status})>"


class AnnualConsultationCredit(Base):
    """
    Track free consultations for annual subscribers.
    Standard Annual: 10 minutes free
    Premium Annual: 20 minutes free
    """
    __tablename__ = 'annual_consultation_credits'
    
    id = Column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(PGUUID(as_uuid=True), ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    subscription_id = Column(PGUUID(as_uuid=True), ForeignKey('user_subscriptions.id', ondelete='CASCADE'), nullable=False)
    
    # Credit details
    consultation_minutes = Column(Integer, nullable=False)  # 10 or 20
    minutes_remaining = Column(Integer, nullable=False)
    
    # Usage tracking
    used = Column(Boolean, default=False)
    used_at = Column(DateTime, nullable=True)
    referral_id = Column(PGUUID(as_uuid=True), ForeignKey('referral_requests.id'), nullable=True)
    
    # Validity period
    valid_from = Column(DateTime, nullable=False)
    valid_until = Column(DateTime, nullable=False)
    expired = Column(Boolean, default=False)
    
    # Timestamps
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    
    def __repr__(self):
        return f"<AnnualConsultationCredit(user_id={self.user_id}, minutes={self.minutes_remaining}/{self.consultation_minutes})>"


class SubscriptionEvent(Base):
    """
    Audit log for subscription events.
    Tracks all subscription-related changes for debugging and analytics.
    """
    __tablename__ = 'subscription_events'
    
    id = Column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(PGUUID(as_uuid=True), ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    subscription_id = Column(PGUUID(as_uuid=True), ForeignKey('user_subscriptions.id', ondelete='SET NULL'), nullable=True)
    
    # Event details
    event_type = Column(String(50), nullable=False)  # 'created', 'upgraded', 'downgraded', 'cancelled', 'renewed', 'expired', 'payment_failed'
    old_plan_id = Column(String(50), nullable=True)
    new_plan_id = Column(String(50), nullable=True)
    
    # Stripe event (if applicable)
    stripe_event_id = Column(String(100), nullable=True)

    # RevenueCat event (if applicable)
    revenuecat_event_id = Column(String(100), nullable=True)
    
    # Additional data
    event_metadata = Column(JSON, nullable=True)  # Named event_metadata to avoid SQLAlchemy reserved 'metadata'
    
    # Timestamp
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)
    
    def __repr__(self):
        return f"<SubscriptionEvent(id={self.id}, user_id={self.user_id}, event={self.event_type})>"


class HomeActionEvent(Base):
    """Tracks selections from the three primary Home Page actions."""
    __tablename__ = 'home_action_events'

    id = Column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(
        PGUUID(as_uuid=True),
        ForeignKey('users.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
    )
    action = Column(String(50), nullable=False, index=True)
    destination = Column(String(50), nullable=False)
    platform = Column(String(20), nullable=True)
    app_version = Column(String(30), nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
        index=True,
    )

    def __repr__(self):
        return f"<HomeActionEvent(id={self.id}, user_id={self.user_id}, action={self.action})>"
