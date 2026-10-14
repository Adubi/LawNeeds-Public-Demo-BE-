"""Pending signup lifecycle. Never persist passwords or plaintext verification codes."""
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import os
import secrets
import uuid

import jwt
from fastapi import HTTPException
from sqlalchemy import func, text

from .models import User, EmailVerificationToken, RegistrationReminderStatus
from .password_hashing import hash_password

RETENTION_DAYS = 7
MAX_ATTEMPTS = 5
RESEND_SECONDS = 60
FIELDS = (
    "user_name email client first_name last_name date_of_birth address town_city country postcode "
    "professional_type sra_bsb_iaa sra_bsb professional_employer legal_expertise proof_of_id "
    "proof_of_id_type firm_name date_of_incorporation practice_areas contact_person "
    "company_registration_number hr_legal_type sector company_name organisation_name "
    "date_of_establishment organisation_type areas_of_legal_focus registration_charity_number"
).split()


def normalize(email):
    return str(email).strip().lower()


def utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def lock_email(session, email):
    # Serialize signup/resend/verify/delete even when no token row exists yet.
    if session.bind.dialect.name == "postgresql":
        key = int.from_bytes(hashlib.sha256(normalize(email).encode()).digest()[:8], "big", signed=True)
        session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


def verifier(email, token_id, code):
    secret = os.getenv("EMAIL_VERIFICATION_SECRET") or os.getenv("SECRET_KEY")
    if not secret:
        raise RuntimeError("Email verification secret is not configured")
    payload = f"registration-v1:{normalize(email)}:{token_id}:{code}"
    return "hmac-v1:" + hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def clear_registration(session, email):
    email = normalize(email)
    lock_email(session, email)
    session.query(EmailVerificationToken).filter(
        func.lower(func.trim(EmailVerificationToken.email)) == email
    ).delete(synchronize_session=False)
    session.query(RegistrationReminderStatus).filter(
        func.lower(func.trim(RegistrationReminderStatus.email)) == email
    ).delete(synchronize_session=False)


def issue(db, email, data=None):
    """data is provided only by signup; resend loads the saved hash internally."""
    email = normalize(email)
    now = datetime.now(timezone.utc)
    with db.get_session() as session:
        lock_email(session, email)
        existing = session.query(EmailVerificationToken).filter(
            EmailVerificationToken.email == email, EmailVerificationToken.used.is_(False)
        ).order_by(EmailVerificationToken.created_at.desc()).with_for_update().first()
        if existing and (existing.attempts or 0) >= MAX_ATTEMPTS and utc(existing.expires_at) > now:
            raise HTTPException(429, "Too many attempts; wait until the current code expires")
        if existing and utc(existing.created_at) > now - timedelta(seconds=RESEND_SECONDS):
            raise HTTPException(429, "Please wait one minute before requesting another code")
        status = session.query(RegistrationReminderStatus).filter_by(email=email).first()
        if status and status.started_at and utc(status.started_at) < now - timedelta(days=RETENTION_DAYS):
            clear_registration(session, email)
            session.flush()
            existing = status = None
        if data is None:
            if not existing or not existing.registration_data:
                raise HTTPException(404, "No pending registration found; please start registration again")
            saved = dict(existing.registration_data)
            # Transition support for unmigrated pending rows, never store plaintext again.
            if "password" in saved:
                saved["password_hash"] = hash_password(saved.pop("password"))
        else:
            saved = {k: data[k] for k in FIELDS if k in data}
            saved["password_hash"] = hash_password(data["password"])
        saved["email"] = email
        if not saved.get("password_hash"):
            raise HTTPException(400, "Please start registration again")
        if session.query(User.id).filter(
            (func.lower(User.email) == email) | (User.user_name == saved.get("user_name"))
        ).first():
            raise HTTPException(400, "User with this email or username already exists")
        # Every issuance invalidates all prior codes, including still-valid codes.
        session.query(EmailVerificationToken).filter(
            EmailVerificationToken.email == email
        ).delete(synchronize_session=False)
        code = f"{secrets.randbelow(1000000):06d}"
        token_id = uuid.uuid4()
        expires = now + timedelta(minutes=15)
        session.add(EmailVerificationToken(
            id=token_id, email=email, token_hash=verifier(email, token_id, code),
            otp_code=None, attempts=0, used=False, created_at=now,
            expires_at=expires, registration_data=saved,
        ))
        if status is None:
            status = RegistrationReminderStatus(email=email, started_at=now)
            session.add(status)
        status.first_name = saved.get("first_name")
        status.professional_type = saved.get("professional_type")
        if status.started_at is None:
            status.started_at = now
    return {"otp": code, "email": email, "expires": expires.isoformat()}


def complete(db, email, code):
    email = normalize(email)
    now = datetime.now(timezone.utc)
    invalid = False
    result = None
    with db.get_session() as session:
        lock_email(session, email)
        token = session.query(EmailVerificationToken).filter(
            EmailVerificationToken.email == email, EmailVerificationToken.used.is_(False)
        ).order_by(EmailVerificationToken.created_at.desc()).with_for_update().first()
        if (not token or utc(token.expires_at) <= now or (token.attempts or 0) >= MAX_ATTEMPTS
                or not token.token_hash.startswith("hmac-v1:")):
            invalid = True
        elif (len(code) != 6 or not code.isascii() or not code.isdigit()
              or not hmac.compare_digest(token.token_hash, verifier(email, token.id, code))):
            token.attempts = (token.attempts or 0) + 1
            invalid = True
        else:
            data = dict(token.registration_data or {})
            password_hash = data.get("password_hash")
            if not password_hash:
                invalid = True
            else:
                if session.query(User.id).filter(
                    (func.lower(User.email) == email) | (User.user_name == data.get("user_name"))
                ).first():
                    raise HTTPException(409, "An account already exists")
                values = {key: data[key] for key in FIELDS if key in data}
                for key in ("date_of_birth", "date_of_incorporation", "date_of_establishment"):
                    if values.get(key):
                        values[key] = datetime.fromisoformat(values[key]).date()
                values["email"] = email
                user = User(**values, hashed_password=password_hash, is_email_verified=True)
                session.add(user)
                session.flush()
                details = {key: getattr(user, key) for key in (
                    "email", "first_name", "last_name", "user_name", "professional_type"
                )}
                details["id"] = str(user.id)
                access_token = jwt.encode(
                    {"email": email, "user_id": str(user.id)}, os.environ["SECRET_KEY"], algorithm="HS256"
                )
                clear_registration(session, email)
                result = {"token": access_token, "user": details,
                          "user_type": user.professional_type or "User",
                          "message": "Registration completed successfully"}
    # Raise after commit so failed-attempt counters survive the error response.
    if invalid:
        raise HTTPException(400, "Invalid or expired verification code; request a new code")
    return result


def cleanup(session):
    """Remove completed records and unfinished registrations older than seven days."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
    emails = {row[0] for row in session.query(EmailVerificationToken.email).filter(
        (EmailVerificationToken.used.is_(True)) | (EmailVerificationToken.created_at < cutoff)
    ).all()}
    emails.update(row[0] for row in session.query(RegistrationReminderStatus.email).filter(
        RegistrationReminderStatus.started_at < cutoff
    ).all())
    for email in sorted(emails):
        lock_email(session, email)
        # Recheck after lock: another request may have started a fresh signup.
        latest = session.query(EmailVerificationToken).filter_by(email=email).order_by(
            EmailVerificationToken.created_at.desc()
        ).first()
        status = session.query(RegistrationReminderStatus).filter_by(email=email).first()
        if ((latest and (latest.used or utc(latest.created_at) < utc(cutoff)))
                or (status and status.started_at and utc(status.started_at) < utc(cutoff))):
            clear_registration(session, email)
