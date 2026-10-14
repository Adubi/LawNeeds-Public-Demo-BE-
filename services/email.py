import os
import smtplib
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication
from dotenv import load_dotenv
from email.utils import formataddr

# Load environment variables
load_dotenv()

# Get email configuration from environment
SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USERNAME = os.getenv("SMTP_USERNAME", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
EMAIL_SENDER = os.getenv("EMAIL_SENDER", "noreply@lawneeds.co.uk")
BASE_URL = os.getenv("BASE_URL", "http://localhost:3000")
APP_SCHEME = "com.steve.lawneeds"

logger = logging.getLogger(__name__)


class EmailService:
    @staticmethod
    def send_email(to_email, subject, html_content, text_content=None, attachments=None):
        """
        Send an email with both HTML and plain text content.

        attachments: optional list of dicts, each shaped like
            {"filename": "receipt.pdf", "content": <bytes>, "mimetype": "application/pdf"}
        Currently only application/pdf attachments are exercised (receipts),
        but the shape is generic in case other attachment types are needed later.
        """
        if not all([SMTP_SERVER, SMTP_PORT, SMTP_USERNAME, SMTP_PASSWORD]):
            logger.error("Email configuration is incomplete. Cannot send email.")
            return False

        try:
            # Validate and clean the recipient email
            to_email = (to_email or "").strip().lower()
            
            # Log the exact recipient for debugging
            logger.info(f"Preparing to send email to: {to_email} (subject: {subject})")

            if not to_email:
                logger.error("Recipient email is required. Cannot send email.")
                return False
            
            # Ensure we're not accidentally sending to multiple recipients
            if "," in to_email or ";" in to_email:
                logger.error(f"Multiple recipients detected in to_email: {to_email}")
                raise ValueError("Only single recipient allowed for OTP emails")

            # Create message container. "mixed" is used at the top level so
            # attachments can sit alongside the body; the html/text pair is
            # nested in an "alternative" sub-part, same as the old structure.
            msg = MIMEMultipart("mixed")
            msg["Subject"] = subject
            msg["From"] = formataddr(("LawNeeds", EMAIL_SENDER))
            msg["To"] = to_email

            # Explicitly ensure no BCC or CC is set
            # Do not add any Bcc or Cc headers

            body = MIMEMultipart("alternative")

            # Add plain text version (if provided)
            if text_content:
                part1 = MIMEText(text_content, "plain")
                body.attach(part1)

            # Add HTML version
            part2 = MIMEText(html_content, "html")
            body.attach(part2)

            msg.attach(body)

            # Attach any files (e.g. PDF receipt)
            if attachments:
                for att in attachments:
                    try:
                        part = MIMEApplication(att["content"], _subtype="pdf")
                        part.add_header(
                            "Content-Disposition",
                            "attachment",
                            filename=att["filename"],
                        )
                        msg.attach(part)
                    except Exception as att_err:
                        logger.error(f"Failed to attach {att.get('filename')}: {att_err}")
                        return False

            # Send email
            with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=15) as server:
                server.starttls()
                server.login(SMTP_USERNAME, SMTP_PASSWORD)

                # Log the exact recipient list being used
                logger.info(f"Sending email via SMTP to single recipient: {to_email}")

                # Use explicit recipient list to ensure only one recipient
                server.sendmail(EMAIL_SENDER, [to_email], msg.as_string())

            logger.info(f"Email sent successfully to {to_email} (subject: {subject})")
            return True

        except Exception as e:
            logger.error(f"Failed to send email to {to_email}: {str(e)}")
            return False

    @staticmethod
    def send_password_reset_email(email, otp):
        """
        Send password reset email with OTP code
        """
        from services.email_templates import password_reset

        # Validate recipient email
        email = email.strip().lower()

        # Security check: OTP emails should never go to info@lawneeds.co.uk
        if email == "info@lawneeds.co.uk":
            logger.error(
                "Attempted to send OTP to info@lawneeds.co.uk - blocked for security"
            )
            return False

        subject = "Your LawNeeds Password Reset Code"
        html_content = password_reset.html(otp)
        text_content = password_reset.text(otp)

        return EmailService.send_email(email, subject, html_content, text_content)

    @staticmethod
    def send_email_verification_email(email, otp):
        """
        Send email verification email with OTP code
        """
        from services.email_templates import email_verification

        # Validate recipient email
        email = email.strip().lower()

        # Security check: OTP emails should never go to info@lawneeds.co.uk
        if email == "info@lawneeds.co.uk":
            logger.error(
                "Attempted to send OTP to info@lawneeds.co.uk - blocked for security"
            )
            return False

        subject = "Verify Your LawNeeds Account - Registration Code"
        html_content = email_verification.html(otp)
        text_content = email_verification.text(otp)

        return EmailService.send_email(email, subject, html_content, text_content)

    @staticmethod
    def send_account_created_email(to_email, first_name=None):
        """
        Send welcome email after successful account creation.
        first_name: optional friendly name to personalise the greeting.
        """
        from services.email_templates import account_created

        to_email = (to_email or "").strip().lower()
        first_name = (first_name or "").strip()
        display_name = first_name if first_name else "there"

        subject = "Welcome to LawNeeds – Your account is ready"
        html_content = account_created.html(display_name, BASE_URL)
        text_content = account_created.text(display_name)
 
        return EmailService.send_email(to_email, subject, html_content, text_content)
    
    
    
    @staticmethod
    def send_pro_account_created_email(to_email, first_name=None):
        """
        Send welcome email after successful professional account creation.
        first_name: optional friendly name to personalise the greeting.
        """
        from services.email_templates import account_created_professional

        to_email = (to_email or "").strip().lower()
        first_name = (first_name or "").strip()
        display_name = first_name if first_name else "there"

        subject = "Welcome to LawNeeds – Your professional account is ready"
        html_content = account_created_professional.html(display_name, BASE_URL)
        text_content = account_created_professional.text(display_name)

        return EmailService.send_email(to_email, subject, html_content, text_content)
    
    

    
    @staticmethod
    def send_registration_completion_reminder_email(to_email, first_name=None):
        """
        Send registration completion reminder (24h/72h) for incomplete client registrations.
        first_name: optional friendly name to personalise the greeting.
        """
        from services.email_templates import registration_completion_reminder

        to_email = (to_email or "").strip().lower()
        first_name = (first_name or "").strip()
        display_name = first_name if first_name else "there"

        subject = "You're almost set up – complete your LawNeeds account"
        html_content = registration_completion_reminder.html(display_name, "com.steve.lawneeds://")
        text_content = registration_completion_reminder.text(display_name, "com.steve.lawneeds://")

        return EmailService.send_email(to_email, subject, html_content, text_content)


    @staticmethod
    def send_registration_completion_reminder_professional_email(to_email, first_name=None):
        """
        Send registration completion reminder (24h/72h) for incomplete professional registrations.
        first_name: optional friendly name to personalise the greeting.
        """
        from services.email_templates import registration_completion_reminder_professional

        to_email = (to_email or "").strip().lower()
        first_name = (first_name or "").strip()
        display_name = first_name if first_name else "there"

        subject = "You're almost set up – complete your LawNeeds account"
        html_content = registration_completion_reminder_professional.html(display_name, "com.steve.lawneeds://")
        text_content = registration_completion_reminder_professional.text(display_name, "com.steve.lawneeds://")

        return EmailService.send_email(to_email, subject, html_content, text_content)
        
    
    
    @staticmethod
    def send_account_deletion_email(to_email, first_name=None):
        """
        Send confirmation email after account deletion.
        first_name: optional friendly name to personalise the greeting.
        """
        from services.email_templates import account_deleted

        to_email = (to_email or "").strip().lower()
        first_name = (first_name or "").strip()
        display_name = first_name if first_name else "there"

        subject = "Your LawNeeds account has been deleted"
        html_content = account_deleted.html(display_name)
        text_content = account_deleted.text(display_name)

        return EmailService.send_email(to_email, subject, html_content, text_content)
    
    
    
    @staticmethod
    def send_subscription_cancellation_email(
        to_email,
        first_name=None,
        subscription_name=None,
        subscription_end_date=None,
    ):
        """
        Send confirmation email after a subscription is cancelled.
        first_name: optional friendly name to personalise the greeting.
        subscription_name: display name of the plan that was cancelled
        (e.g. subscription.plan_id resolved to a human label, or the
        plan's display name).
        subscription_end_date: caller-supplied, pre-formatted display
        string for when access actually ends (subscription.current_period_end).
        This method does NOT reformat it - same approach as
        consultation_length in send_lawyer_accepted_your_request_email.

        Trigger points: intended to be called from both live cancellation
        paths - cancel_subscription() (in-app cancel) and the CANCELLATION
        branch of _handle_revenuecat_subscription_event() (RevenueCat
        webhook) in subscription_service.py. handle_stripe_subscription_updated()
        is confirmed dead code (no callers found in the codebase) and is
        NOT wired to this email.
        """
        from services.email_templates import subscription_cancellation

        to_email = (to_email or "").strip().lower()
        first_name = (first_name or "").strip()
        display_name = first_name if first_name else "there"
        subscription_name = subscription_name or "your"
        subscription_end_date = subscription_end_date or "the end of your current billing period"

        subject = "Your LawNeeds subscription has been cancelled"
        html_content = subscription_cancellation.html(
            display_name,
            subscription_name,
            subscription_end_date,
        )
        text_content = subscription_cancellation.text(
            display_name,
            subscription_name,
            subscription_end_date,
        )

        return EmailService.send_email(to_email, subject, html_content, text_content)
    
    
    
    
    @staticmethod
    def send_subscription_renewal_reminder_email(
        to_email,
        first_name=None,
        subscription_name=None,
        renewal_date=None,
        renewal_amount=None,
    ):
        """
        Send a reminder email 3 days before a subscription auto-renews.

        first_name: optional friendly name to personalise the greeting.
        subscription_name: display name of the plan renewing (same
        resolution as subscription_name in
        send_subscription_cancellation_email).
        renewal_date: caller-supplied, pre-formatted display string for
        when the renewal will occur. This method does NOT reformat it -
        same approach as subscription_end_date in
        send_subscription_cancellation_email.
        renewal_amount: RAW numeric amount (no currency symbol), e.g.
        "9.99". This method hardcodes the £ prefix itself, matching the
        spec's literal "£{{RenewalAmount}}" - unlike
        send_subscription_purchase_confirmation_email/
        send_wallet_topup_confirmation_email, there is no currency
        parameter here, since this fires before any RevenueCat/Stripe
        transaction exists to read a currency code from.

        NOTE ON TRIGGERING: per spec this should fire 3 days before each
        subscription's renewal date. This method only sends a single
        email on demand - the recurring job that queries subscriptions
        renewing in 3 days and calls this per eligible user still needs
        to be wired up separately, the same way
        send_identity_verification_reminder_email's cadence is handled
        by a job outside this method.
        """
        from services.email_templates import subscription_renewal_reminder

        to_email = (to_email or "").strip().lower()
        first_name = (first_name or "").strip()
        display_name = first_name if first_name else "there"
        subscription_name = subscription_name or "your"
        renewal_date = renewal_date or "your renewal date"
        renewal_amount = renewal_amount if renewal_amount is not None else "-"

        subject = "Your LawNeeds subscription renews in 3 days"
        html_content = subscription_renewal_reminder.html(
            display_name,
            subscription_name,
            renewal_date,
            renewal_amount,
        )
        text_content = subscription_renewal_reminder.text(
            display_name,
            subscription_name,
            renewal_date,
            renewal_amount,
        )

        return EmailService.send_email(to_email, subject, html_content, text_content)
    
    

    @staticmethod
    def send_identity_verification_reminder_email(to_email, first_name=None):
        """
        Send a reminder email prompting the user to complete identity
        verification via Persona.

        NOTE ON SCHEDULING: per the spec this should go out twice monthly
        (1st and last Monday) and only to users who haven't yet verified
        their account. This method only sends a single email on demand —
        the recurring, filtered-audience job (querying users where
        identity_verification_required() is True and
        identity_verification_approved() is False, then scheduling on that
        cadence) still needs to be wired up separately, e.g. as a cron task
        or scheduled job that calls this method per eligible user.
        first_name: optional friendly name to personalise the greeting.
        """
        from services.email_templates import identity_verification_reminder

        to_email = (to_email or "").strip().lower()
        first_name = (first_name or "").strip()
        display_name = first_name if first_name else "there"

        subject = "Complete your identity verification"
        html_content = identity_verification_reminder.html(display_name)
        text_content = identity_verification_reminder.text(display_name)

        return EmailService.send_email(to_email, subject, html_content, text_content)
    
    @staticmethod
    def send_identity_verification_reminder_professional_email(to_email, first_name=None):
        """
        Send a reminder email prompting a professional user to complete
        identity verification via Persona.

        Professional-account counterpart to
        send_identity_verification_reminder_email. Same twice-monthly
        (1st and last Monday) scheduling note applies - this method only
        sends a single email on demand; the recurring job still needs to
        call this for professional rows and the existing method for
        regular user rows.
        first_name: optional friendly name to personalise the greeting.
        """
        from services.email_templates import identity_verification_reminder_professional

        to_email = (to_email or "").strip().lower()
        first_name = (first_name or "").strip()
        display_name = first_name if first_name else "there"

        subject = "Complete your professional identity verification"
        html_content = identity_verification_reminder_professional.html(display_name)
        text_content = identity_verification_reminder_professional.text(display_name)

        return EmailService.send_email(to_email, subject, html_content, text_content)
    
    

    @staticmethod
    def send_identity_verified_email(to_email, first_name=None):
        """
        Send confirmation email after successful identity verification (Persona).
        first_name: optional friendly name to personalise the greeting.
        """
        from services.email_templates import identity_verification_successful

        to_email = (to_email or "").strip().lower()
        first_name = (first_name or "").strip()
        display_name = first_name if first_name else "there"

        subject = "Your identity has been successfully verified"
        html_content = identity_verification_successful.html(display_name)
        text_content = identity_verification_successful.text(display_name)

        return EmailService.send_email(to_email, subject, html_content, text_content)
    
    
    @staticmethod
    def send_identity_verified_professional_email(to_email, first_name=None):
        """
        Send confirmation email after successful identity verification (Persona)
        for a professional account.

        Professional-account counterpart to send_identity_verified_email, same
        relationship as send_identity_verification_reminder_professional_email
        is to send_identity_verification_reminder_email - separate template,
        separate subject line, same trigger shape (caller wires this in from
        wherever _apply_status distinguishes a professional user from a
        regular one).
        first_name: optional friendly name to personalise the greeting.
        """
        from services.email_templates import identity_verification_successful_professional

        to_email = (to_email or "").strip().lower()
        first_name = (first_name or "").strip()
        display_name = first_name if first_name else "there"

        subject = "Your professional identity has been successfully verified"
        html_content = identity_verification_successful_professional.html(display_name)
        text_content = identity_verification_successful_professional.text(display_name)

        return EmailService.send_email(to_email, subject, html_content, text_content)

    @staticmethod
    def send_pending_action_email(to_email, first_name=None, pending_action=None):
        """
        Send a reminder email for an unfinished (pending) action on the
        user's account.
        first_name: optional friendly name to personalise the greeting.
        pending_action: human-readable description of the incomplete
        activity (e.g. "Identity verification"). Required for the copy
        to make sense; callers should not invoke this with None in
        production, but we don't hard-fail here since send_email() is
        the layer that already validates config/recipient.

        NOTE ON TRIGGERING: per spec this fires 24h after abandonment,
        with a follow-up at 72h if still incomplete, and must not fire
        at all if the action was completed in the meantime. This method
        only sends a single email on demand — the recurring job that
        detects abandonment, applies the 24h/72h timing, checks
        completion status, and marks each stage as sent (so it isn't
        repeated) still needs to be wired up separately, the same way
        send_identity_verification_reminder_email's cadence is handled
        by a job outside this method.
        """
        from services.email_templates import complete_pending_action

        to_email = (to_email or "").strip().lower()
        first_name = (first_name or "").strip()
        display_name = first_name if first_name else "there"
        pending_action = pending_action or "an unfinished activity"

        subject = "You have a pending action on your LawNeeds account"
        html_content = complete_pending_action.html(display_name, pending_action)
        text_content = complete_pending_action.text(display_name, pending_action)

        return EmailService.send_email(to_email, subject, html_content, text_content)

    @staticmethod
    def send_we_miss_you_email(to_email, first_name=None):
        """
        Send the 14-day "We Miss You" re-engagement email.
        first_name: optional friendly name to personalise the greeting.

        NOTE ON SCOPE: per the spec, this re-engagement flow has FOUR
        stages (14 / 30 / 60 / 90 days), each with different copy and
        intent (friendly nudge -> feature highlight -> stronger message
        -> final email before reducing frequency). Only the 14-day copy
        was provided in the spec, so this method only covers that stage.
        The 30/60/90-day copy has not been supplied and must not be
        invented — those stages (and their EmailService methods) are
        NOT built yet, pending that copy.

        NOTE ON TRIGGERING: per the spec this fires based on "last used
        LawNeeds," but no single field in services/models.py has been
        confirmed yet as the source of truth for that (candidates:
        Conversation.last_activity, ChatMessage.created_at, User.updated_at,
        DeviceToken.last_used_at, or a login/session table -- still under
        investigation). This method only sends a single email on demand,
        the same way send_pending_action_email does -- the recurring job
        that determines who has crossed the 14-day threshold, dedupes so
        it isn't resent, and coordinates with the (not yet built) 30/60/90
        stages still needs to be wired up separately, once the activity
        tracking question above is resolved.
        """
        from services.email_templates import we_miss_you

        to_email = (to_email or "").strip().lower()
        first_name = (first_name or "").strip()
        display_name = first_name if first_name else "there"

        subject = "We've missed you – Your LawNeeds account is waiting"
        html_content = we_miss_you.html(display_name)
        text_content = we_miss_you.text(display_name)

        return EmailService.send_email(to_email, subject, html_content, text_content)

    
    
    @staticmethod
    def send_we_miss_you_professional_email(to_email, first_name=None):
        """
        Send the 14-day "We Miss You" re-engagement email for professional
        (lawyer) accounts.

        Professional-account counterpart to send_we_miss_you_email - same
        14-day-only scope note applies: only this stage's copy was
        supplied, 30/60/90-day stages are NOT built pending that copy.
        Same trigger/activity-tracking caveat applies too - the recurring
        job that determines eligibility and dedupes sends still needs to
        be wired up (or the existing we_miss_you job needs to branch by
        account type), once the activity tracking question is resolved.
        first_name: optional friendly name to personalise the greeting.
        """
        from services.email_templates import we_miss_you_professional

        to_email = (to_email or "").strip().lower()
        first_name = (first_name or "").strip()
        display_name = first_name if first_name else "there"

        subject = "We've missed you – Your LawNeeds professional account is waiting"
        html_content = we_miss_you_professional.html(display_name)
        text_content = we_miss_you_professional.text(display_name)

        return EmailService.send_email(to_email, subject, html_content, text_content)
    
    
    
    @staticmethod
    def send_subscription_purchase_confirmation_email(
        to_email,
        first_name=None,
        subscription_name=None,
        billing_cycle=None,
        minutes_included=None,
        renewal_date=None,
        transaction_id=None,
        payment_date=None,
        total_paid=None,
        currency=None,
    ):
        """
        Send confirmation email after a successful subscription purchase,
        with a PDF receipt attached (see services/receipt_pdf.py).
        first_name: optional friendly name to personalise the greeting.
        currency: ISO currency code from RevenueCat (e.g. "GBP", "USD").
        Defaults to GBP display if not provided, since that's LawNeeds'
        primary market.

        NOTE: payment_method/payment_description/subtotal/vat_amount/
        receipt_number were dropped from this signature — all subscription
        purchases go through RevenueCat (App Store / Google Play IAP), and
        that flow only exposes a single total amount, no payment method or
        VAT split.
        """
        from services.email_templates import subscription_purchase_confirmation
        from services.receipt_pdf import generate_receipt_pdf, get_currency_symbol

        to_email = (to_email or "").strip().lower()
        first_name = (first_name or "").strip()
        display_name = first_name if first_name else "there"

        currency_symbol = get_currency_symbol(currency)
        total_paid_display = f"{currency_symbol}{total_paid}" if total_paid is not None else "-"

        subject = "Your LawNeeds subscription is now active"
        html_content = subscription_purchase_confirmation.html(
            display_name,
            subscription_name,
            billing_cycle,
            minutes_included,
            renewal_date,
            transaction_id,
            payment_date,
            total_paid_display,
        )
        text_content = subscription_purchase_confirmation.text(
            display_name,
            subscription_name,
            billing_cycle,
            minutes_included,
            renewal_date,
            transaction_id,
            payment_date,
            total_paid_display,
        )

        # Build the PDF receipt.
        # Mapping: purchase type <- subscription_name, order id <- transaction_id,
        # date <- payment_date, amount paid / total <- total_paid (in the
        # purchase's own currency), support contact hardcoded. No payment
        # method / subtotal / VAT rows.
        receipt_pdf_bytes = generate_receipt_pdf(
            purchase_type=subscription_name,
            order_id=transaction_id,
            date=payment_date,
            amount_paid=total_paid_display,
            support_contact="support@lawneeds.co.uk",
            total_paid=total_paid,
            currency_symbol=currency_symbol,
        )
        attachments = [
            {
                "filename": f"LawNeeds-Receipt-{transaction_id or 'receipt'}.pdf",
                "content": receipt_pdf_bytes,
                "mimetype": "application/pdf",
            }
        ]

        return EmailService.send_email(
            to_email, subject, html_content, text_content, attachments=attachments
        )

    @staticmethod
    def send_wallet_topup_confirmation_email(
        to_email,
        first_name=None,
        minutes_added=None,
        updated_balance=None,
        transaction_id=None,
        payment_date=None,
        total_paid=None,
        currency=None,
    ):
        """
        Send confirmation email after a successful wallet top-up, with a
        PDF receipt attached (see services/receipt_pdf.py).
        first_name: optional friendly name to personalise the greeting.
        currency: ISO currency code from RevenueCat (e.g. "GBP", "USD").
        Defaults to GBP display if not provided.

        NOTE: top-ups go through RevenueCat IAP (confirmed via
        REVENUECAT_TOP_UP_PRODUCTS / sync_consumable_from_client in
        subscription_service.py), same as subscriptions - so this
        signature deliberately excludes payment_method/subtotal/vat_amount/
        receipt_number, same reasoning as
        send_subscription_purchase_confirmation_email.

        updated_balance: minutes balance after this top-up is applied
        (e.g. account.top_up_minutes), NOT a currency figure - this app
        tracks a minutes balance, not a cash wallet.
        """
        from services.email_templates import wallet_topup_confirmation
        from services.receipt_pdf import generate_receipt_pdf, get_currency_symbol

        to_email = (to_email or "").strip().lower()
        first_name = (first_name or "").strip()
        display_name = first_name if first_name else "there"

        currency_symbol = get_currency_symbol(currency)
        total_paid_display = f"{currency_symbol}{total_paid}" if total_paid is not None else "-"
        updated_balance_display = str(updated_balance) if updated_balance is not None else "-"

        subject = "Your LawNeeds wallet has been topped up"
        html_content = wallet_topup_confirmation.html(
            display_name,
            minutes_added,
            updated_balance_display,
            transaction_id,
            payment_date,
            total_paid_display,
        )
        text_content = wallet_topup_confirmation.text(
            display_name,
            minutes_added,
            updated_balance_display,
            transaction_id,
            payment_date,
            total_paid_display,
        )

        # Build the PDF receipt.
        # Mapping: purchase type <- "Wallet Top-up", order id <- transaction_id,
        # date <- payment_date, amount paid / total <- total_paid, support
        # contact hardcoded. No payment method / subtotal / VAT rows.
        receipt_pdf_bytes = generate_receipt_pdf(
            purchase_type="Wallet Top-up",
            order_id=transaction_id,
            date=payment_date,
            amount_paid=total_paid_display,
            support_contact="support@lawneeds.co.uk",
            total_paid=total_paid,
            currency_symbol=currency_symbol,
        )
        attachments = [
            {
                "filename": f"LawNeeds-Receipt-{transaction_id or 'receipt'}.pdf",
                "content": receipt_pdf_bytes,
                "mimetype": "application/pdf",
            }
        ]

        return EmailService.send_email(
            to_email, subject, html_content, text_content, attachments=attachments
        )

    @staticmethod
    def send_consultation_payment_confirmation_email(
        to_email,
        first_name=None,
        lawyer_name=None,
        consultation_reference=None,
        consultation_date=None,
        consultation_time=None,
        consultation_length=None,
        receipt_number=None,
        transaction_id=None,
        payment_date=None,
        subtotal=None,
        vat_amount=None,
        total_paid=None,
    ):
        """
        Send confirmation email after a successful consultation payment,
        with a PDF receipt attached (see services/receipt_pdf.py).

        SCOPE: single_consultation and ongoing_support only. Casework
        payments (ConsultationPaymentPricingType.CASEWORK) have no booked
        slot at all (booking_service.requires_booking_slot() returns False
        for casework), so there is no LawyerName/ConsultationDate/Time to
        populate here - a casework confirmation email is a separate,
        not-yet-built automation. Callers must not invoke this method for
        casework payments.

        NOTE: payment_method was dropped from this signature - it is not
        stored anywhere on ConsultationPayment or exposed by
        ConsultationPaymentService.get_payment_summary(), same reasoning
        as #5/#6. UNLIKE #5/#6, subtotal/vat_amount/total_paid ARE real
        here - consultations go through Stripe with a genuine net/VAT/gross
        split (ConsultationPayment.net_amount_pence / vat_amount_pence /
        gross_amount_pence), not RevenueCat.

        subtotal/vat_amount/total_paid: RAW GBP amounts, no currency
        symbol (e.g. "83.33", or a Decimal/float) - pass
        get_payment_summary()'s net_amount_gbp / vat_amount_gbp /
        gross_amount_gbp values directly. This method builds the £-prefixed
        display strings for the email itself; generate_receipt_pdf() also
        prefixes total_paid with currency_symbol internally, so passing an
        already-formatted "£83.33" string here would double up the symbol
        in the PDF - same mistake as the base64-decode issue in #6, just
        in the PDF total box instead of the email body.
        receipt_number: caller should pass str(payment.id) - there is no
        dedicated receipt-number field on ConsultationPayment.
        consultation_reference: caller should pass str(session_id) (the
        ReferralSession id) - the identifier the user's app UI actually
        shows for this consultation, as opposed to the billing-only
        payment.id used for receipt_number.
        lawyer_name: caller must resolve this from the User row (only
        lawyer_id is available on the session/payment).
        consultation_date/consultation_time/consultation_length: caller
        must resolve these from ConsultationBookingRequest
        (requested_start_at / requested_end_at) via booking_service -
        these are not present on ConsultationPayment.
        """
        from services.email_templates import consultation_payment_confirmation
        from services.receipt_pdf import generate_receipt_pdf, get_currency_symbol

        to_email = (to_email or "").strip().lower()
        first_name = (first_name or "").strip()
        display_name = first_name if first_name else "there"
        lawyer_name = lawyer_name or "your lawyer"

        currency_symbol = get_currency_symbol("GBP")
        subtotal_display = f"{currency_symbol}{subtotal}" if subtotal is not None else "-"
        vat_amount_display = f"{currency_symbol}{vat_amount}" if vat_amount is not None else "-"
        total_paid_display = f"{currency_symbol}{total_paid}" if total_paid is not None else "-"

        subject = "Your consultation has been confirmed"
        html_content = consultation_payment_confirmation.html(
            display_name,
            lawyer_name,
            consultation_reference,
            consultation_date,
            consultation_time,
            consultation_length,
            receipt_number,
            transaction_id,
            payment_date,
            subtotal_display,
            vat_amount_display,
            total_paid_display,
        )
        text_content = consultation_payment_confirmation.text(
            display_name,
            lawyer_name,
            consultation_reference,
            consultation_date,
            consultation_time,
            consultation_length,
            receipt_number,
            transaction_id,
            payment_date,
            subtotal_display,
            vat_amount_display,
            total_paid_display,
        )

        # Build the PDF receipt. total_paid stays RAW here - generate_receipt_pdf
        # prepends currency_symbol itself (see its TOTAL PAID box logic).
        receipt_pdf_bytes = generate_receipt_pdf(
            purchase_type=f"Consultation with {lawyer_name}",
            order_id=transaction_id,
            date=payment_date,
            amount_paid=total_paid_display,
            support_contact="support@lawneeds.co.uk",
            total_paid=total_paid,
            currency_symbol=currency_symbol,
        )
        attachments = [
            {
                "filename": f"LawNeeds-Receipt-{receipt_number or 'receipt'}.pdf",
                "content": receipt_pdf_bytes,
                "mimetype": "application/pdf",
            }
        ]

        return EmailService.send_email(
            to_email, subject, html_content, text_content, attachments=attachments
        )
        
        html_content = f"""
        <html>
        <head>
            <style>
                body {{ font-family: Arial, sans-serif; line-height: 1.6; color: #333; }}
                .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
                .logo {{ text-align: center; margin-bottom: 20px; }}
                .otp-code {{ display: inline-block; background-color: #f0f0f0; color: #333; 
                            padding: 15px 25px; font-size: 24px; font-weight: bold; 
                            border-radius: 8px; letter-spacing: 3px; text-align: center;
                            margin: 20px 0; border: 2px dashed #4CAF50; }}
                .highlight {{ background-color: #e8f5e8; padding: 15px; border-radius: 4px; 
                             border-left: 4px solid #4CAF50; margin: 20px 0; }}
                .footer {{ margin-top: 30px; font-size: 12px; color: #777; text-align: center; }}
                .warning {{ color: #d32f2f; font-weight: bold; }}
                .welcome {{ background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); 
                           color: white; padding: 20px; border-radius: 10px; text-align: center; }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="logo">
                    <h2>LawNeeds</h2>
                </div>
                
                <div class="welcome">
                    <h3>🎉 Welcome to LawNeeds!</h3>
                    <p>You're just one step away from accessing our platform</p>
                </div>
                
                <p>Hello,</p>
                <p>Thank you for registering with LawNeeds! To complete your account setup and 
                   gain access to our platform, please verify your email address.</p>
                
                <div class="highlight">
                    <p><strong>Your email verification code is:</strong></p>
                    <div style="text-align: center;">
                        <div class="otp-code">{otp}</div>
                    </div>
                </div>
                
                <p>To complete your registration:</p>
                <ol>
                    <li>Return to the registration page</li>
                    <li>Enter this verification code when prompted</li>
                    <li>Complete your account setup</li>
                    <li>Start exploring LawNeeds!</li>
                </ol>
                
                <p class="warning">Important:</p>
                <ul>
                    <li>This code will expire in 15 minutes for security reasons</li>
                    <li>Do not share this code with anyone</li>
                    <li>If you didn't create this account, please ignore this email</li>
                </ul>
                
                <p>Once verified, you'll have full access to:</p>
                <ul>
                    <li>📚 Legal knowledge bases and resources</li>
                    <li>💬 AI-powered legal chat assistance</li>
                    <li>🔍 Professional legal services directory</li>
                    <li>📋 Profile management and networking tools</li>
                </ul>
                
                <p>Welcome aboard!<br>The LawNeeds Team</p>
                <div class="footer">
                    <p>This is an automated message, please do not reply to this email.</p>
                    <p>Need help? Contact us at <a href="mailto:support@lawneeds.co.uk">support@lawneeds.co.uk</a></p>
                </div>
            </div>
        </body>
        </html>
        """
        
        text_content = f"""
        Welcome to LawNeeds!
        
        Thank you for registering with LawNeeds! To complete your account setup and 
        gain access to our platform, please verify your email address.

        Your email verification code is: {otp}

        To complete your registration:
        1. Return to the registration page
        2. Enter this verification code when prompted
        3. Complete your account setup
        4. Start exploring LawNeeds!

        IMPORTANT:
        - This code will expire in 15 minutes for security reasons
        - Do not share this code with anyone
        - If you didn't create this account, please ignore this email

        Once verified, you'll have full access to:
        - Legal knowledge bases and resources
        - AI-powered legal chat assistance
        - Professional legal services directory
        - Profile management and networking tools

        Welcome aboard!
        The LawNeeds Team

        This is an automated message, please do not reply to this email.
        Need help? Contact us at support@lawneeds.co.uk
        """
        
        return EmailService.send_email(email, subject, html_content, text_content) 

    @staticmethod
    def send_request_service_submitted_email(
        to_email,
        first_name=None,
        lawyer_name=None,
        urgency_level=None,
    ):
        """
        Send confirmation email after a Request Service enquiry has been
        delivered to a matched lawyer (automation #9).
        """
        from services.email_templates import request_service_submitted

        to_email = (to_email or "").strip().lower()
        first_name = (first_name or "").strip()
        display_name = first_name if first_name else "there"
        lawyer_name = lawyer_name or "your lawyer"

        urgency_labels = {
            "urgent": "Urgent",
            "inferred_urgent": "Urgent",
            "normal": "Normal",
            "flexible": "Flexible",
        }
        urgency_label = urgency_labels.get((urgency_level or "").lower(), "Normal")

        subject = "Your request has been sent"
        html_content = request_service_submitted.html(
            display_name,
            lawyer_name,
            urgency_label,
        )
        text_content = request_service_submitted.text(
            display_name,
            lawyer_name,
            urgency_label,
        )

        return EmailService.send_email(to_email, subject, html_content, text_content)

    @staticmethod
    def send_user_referral_request_submitted_email(
        to_email,
        first_name=None,
        lawyer_name=None,
        urgency_level=None,
    ):
        """
        Send confirmation email after the chatbot's lawyer referral flow
        completes a selection (automation #10).
        """
        from services.email_templates import user_referral_request_submitted

        to_email = (to_email or "").strip().lower()
        first_name = (first_name or "").strip()
        display_name = first_name if first_name else "there"
        lawyer_name = lawyer_name or "your lawyer"

        urgency_labels = {
            "urgent": "Urgent",
            "inferred_urgent": "Urgent",
            "normal": "Normal",
            "flexible": "Flexible",
        }
        urgency_label = urgency_labels.get((urgency_level or "").lower(), "Normal")

        subject = "Your referral request has been sent"
        html_content = user_referral_request_submitted.html(
            display_name,
            lawyer_name,
            urgency_label,
        )
        text_content = user_referral_request_submitted.text(
            display_name,
            lawyer_name,
            urgency_label,
        )

        return EmailService.send_email(to_email, subject, html_content, text_content)
    
    
    @staticmethod
    def send_user_referral_request_received_professional_email(
        to_email,
        first_name=None,
        legal_area=None,
        budget_range=None,
        casework_budget_gbp=None,
        urgency_level=None,
    ):
        """
        Notify a lawyer that a new chatbot referral request has come in and
        is awaiting their response (automation #6).

        Professional-account counterpart to #10
        (send_user_referral_request_submitted_email) - same referral, but
        this is the notification to the LAWYER that a request is waiting
        for them, not the confirmation to the USER that their request went
        out. Design imitates account_created's template (band header +
        details table + callout box), same as #9/#10 and the other
        "_professional" counterparts (e.g.
        send_consultation_payment_confirmation_professional_email).

        FIELD MAPPING - confirmed against services/models.py and
        api/referral.py:
        - legal_area: caller passes ReferralRequest.legal_area (the raw
          short code, e.g. "immigration"). This method converts it to the
          display label using the same area_mapping dict that
          match_lawyers_to_referral (api/referral.py) already uses for
          matching, so the label the lawyer sees matches what determined
          the match.
        - budget_range / casework_budget_gbp: mirrors the "Budget" line
          built in api/app.py (~line 2549-2566): budget_range is passed
          through services.referral_display_helpers.get_budget_range_display()
          for the normal case; if casework_budget_gbp is not None, it
          overrides that with a raw "£{amount:,}" display instead - same
          precedence as app.py. ongoing_budget_gbp is deliberately not
          used here, since app.py's own Budget line doesn't fall back to
          it either (get_budget_range_display already covers the
          ongoing-support buckets via budget_range).
        - urgency_level: same enum/mapping as #9/#10
          (ReferralRequest.urgency_level -> UrgencyLevel enum: urgent /
          normal / flexible / inferred_urgent). Reused verbatim below.

        EstimatedResponseTime: DROPPED from this template. Confirmed via
        repo-wide search - the only source hits are third-party packages
        and this codebase's own test assertions that it's absent
        (test_estimated_response_time_is_not_present,
        "EstimatedResponseTime" not in html_body), so this carries over
        the #9/#10 finding as fully re-confirmed for this flow too, not
        just assumed.

        Trigger point: not yet wired up. match_lawyers_to_referral
        (api/referral.py, ~line 1079) is the confirmed entry point that
        builds the matched-lawyer query, but where it actually creates
        each matched lawyer's inbox row (the real per-lawyer hook for this
        email) hasn't been confirmed yet - still need to see past its
        budget/expertise filtering to the loop that persists inbox items
        before wiring this in live.
        """
        from services.email_templates import user_referral_request_received_professional
        from services.referral_display_helpers import get_budget_range_display

        to_email = (to_email or "").strip().lower()
        first_name = (first_name or "").strip()
        display_name = first_name if first_name else "there"

        area_mapping = {
            "immigration": "Immigration Law",
            "family": "Family Law",
            "employment": "Employment Law",
            "road_traffic": "Road Traffic Law",
            "road_traffic_offences": "Road Traffic Law",
        }
        area_of_law_display = area_mapping.get(legal_area, legal_area) or "-"

        if casework_budget_gbp is not None:
            budget_display = f"£{int(casework_budget_gbp):,}"
        else:
            budget_display = get_budget_range_display(budget_range)

        urgency_labels = {
            "urgent": "Urgent",
            "inferred_urgent": "Urgent",
            "normal": "Normal",
            "flexible": "Flexible",
        }
        urgency_label = urgency_labels.get((urgency_level or "").lower(), "Normal")

        subject = "You've received a new referral request on LawNeeds"
        html_content = user_referral_request_received_professional.html(
            display_name,
            area_of_law_display,
            budget_display,
            urgency_label,
            "com.steve.lawneeds://",
        )
        text_content = user_referral_request_received_professional.text(
            display_name,
            area_of_law_display,
            budget_display,
            urgency_label,
        )

        return EmailService.send_email(to_email, subject, html_content, text_content)
  
  
    
    @staticmethod
    def send_lawyer_accepted_your_request_email(
        to_email,
        first_name=None,
        lawyer_name=None,
        consultation_length=None,
        session_id=None,
    ):
        """
        Send confirmation email after a lawyer accepts a referral request
        (automation #11), prompting the user to complete payment.

        Trigger point: caller must invoke this only from the "accept" branch
        of POST /referral/lawyer_response (api/referral.py), after
        inbox_entry.status is set to 'accepted' and the transaction is
        committed. This is a separate event from #9/#10 (user selecting a
        lawyer) - do not confuse the two.

        LESSON 3: no payment has happened yet at this point, so unlike
        #5-#8 there is no PDF receipt/attachment and no subtotal/VAT/
        total_paid fields on this method.

        consultation_length: caller-supplied, pre-formatted display string
        (e.g. from consultation_duration_minutes(referral.consultation_type)).
        This method does NOT reformat it - exact display format is still
        being confirmed, so it's passed through as-is rather than guessed
        here.
        lawyer_name: caller must resolve this from the accepting lawyer's
        User row - only lawyer_id is available on the inbox/referral objects.
        """
        from services.email_templates import lawyer_accepted_your_request

        to_email = (to_email or "").strip().lower()
        first_name = (first_name or "").strip()
        display_name = first_name if first_name else "there"
        lawyer_name = lawyer_name or "your lawyer"
        consultation_length = consultation_length or "your"
        redirect_page_url = f"{BASE_URL}/referral/payment_redirect/{session_id}"

        subject = "Great news! Your request has been accepted"
        html_content = lawyer_accepted_your_request.html(
            display_name,
            lawyer_name,
            consultation_length,
            redirect_page_url,
        )
        text_content = lawyer_accepted_your_request.text(
            display_name,
            lawyer_name,
            consultation_length,
        )

        return EmailService.send_email(to_email, subject, html_content, text_content)
    
    
    
    @staticmethod
    def send_support_request_received_email(to_email, first_name=None, feedback_id=None):
        """
        Send confirmation email after a user submits a support/contact
        request (automation #12).

        NOTE ON TRIGGER: despite the "support request" naming, this maps
        to the existing beta feedback flow - the app's "Contact Support"
        screen (app/(app)/manage/support.tsx) submits to the same
        UserService.SubmitFeedback -> POST /feedback/submit endpoint as
        the beta feedback form. There is no separate support-ticket
        system. Caller must invoke this from api/feedback.py's
        submit_feedback, after the BetaFeedback row is committed -
        alongside the existing internal _send_feedback_email call, not
        replacing it (that one notifies the LawNeeds team; this one
        notifies the user - different audiences, not a duplicate).

        feedback_id: the BetaFeedback row's id (a UUID - see
        services/models.py). This method derives an 8-character short
        reference from it for display, since there is no dedicated
        human-readable ticket-number field on BetaFeedback and the spec's
        {{TicketID}} has no other source to map to.
        """
        from services.email_templates import support_request_received

        to_email = (to_email or "").strip().lower()
        first_name = (first_name or "").strip()
        display_name = first_name if first_name else "there"
        ticket_reference = str(feedback_id)[:8] if feedback_id else "-"

        subject = "We've received your support request"
        html_content = support_request_received.html(display_name, ticket_reference)
        text_content = support_request_received.text(display_name, ticket_reference)

        return EmailService.send_email(to_email, subject, html_content, text_content)
    
    
    @staticmethod
    def send_consultation_payment_confirmation_professional_email(
        to_email,
        first_name=None,
        client_name=None,
        consultation_reference=None,
        consultation_date=None,
        consultation_time=None,
        consultation_length=None,
    ):
        """
        Send confirmation email to the professional (lawyer) after a
        consultation payment has been confirmed by the client.

        Professional-account counterpart to
        send_consultation_payment_confirmation_email. Unlike that method,
        this carries no PDF receipt and no subtotal/VAT/total split - the
        professional is not the paying party, so there's nothing to
        receipt. Design imitates account_created's template (band header
        + callout box), per spec item #4.

        client_name: caller must resolve this from the paying client's
        User row - only lawyer_id/client_id are available on the
        session/payment objects, same resolution pattern as lawyer_name
        in send_consultation_payment_confirmation_email.
        consultation_reference/date/time/length: same resolution as their
        counterparts in send_consultation_payment_confirmation_email -
        reference <- str(session_id), date/time/length resolved via
        booking_service from ConsultationBookingRequest.
        """
        from services.email_templates import consultation_payment_confirmation_professional

        to_email = (to_email or "").strip().lower()
        first_name = (first_name or "").strip()
        display_name = first_name if first_name else "there"
        client_name = client_name or "your client"

        subject = "New consultation confirmed on LawNeeds"
        html_content = consultation_payment_confirmation_professional.html(
            display_name,
            client_name,
            consultation_reference,
            consultation_date,
            consultation_time,
            consultation_length,
        )
        text_content = consultation_payment_confirmation_professional.text(
            display_name,
            client_name,
            consultation_reference,
            consultation_date,
            consultation_time,
            consultation_length,
        )

        return EmailService.send_email(to_email, subject, html_content, text_content)
    
    
    
    @staticmethod
    def send_consultation_extension_confirmation_professional_email(
        to_email,
        first_name=None,
        client_first_name=None,
        consultation_reference=None,
        additional_minutes=None,
        new_consultation_end_time=None,
    ):
        """
        Send confirmation email to the professional (lawyer) after an
        ongoing consultation is successfully extended.

        Professional-account counterpart to whatever client-facing
        extension confirmation automation #8 turns out to be (still being
        built separately). No PDF receipt / no payment fields - an
        extension isn't a payment event on its own, same reasoning as
        send_consultation_payment_confirmation_professional_email. Design
        imitates account_created's template (band header + callout box),
        per spec item #5.

        client_first_name: spec uses {{ClientFirstName}} (first name only,
        not the full name used for client_name in
        send_consultation_payment_confirmation_professional_email) -
        caller should resolve this from the client User row accordingly.
        additional_minutes/new_consultation_end_time: caller-supplied,
        pre-formatted display strings - this method does not reformat
        them, same approach as consultation_length in
        send_lawyer_accepted_your_request_email.
        """
        from services.email_templates import consultation_extension_confirmation_professional

        to_email = (to_email or "").strip().lower()
        first_name = (first_name or "").strip()
        display_name = first_name if first_name else "there"
        client_first_name = client_first_name or "your client"

        subject = "Your LawNeeds consultation has been extended"
        html_content = consultation_extension_confirmation_professional.html(
            display_name,
            client_first_name,
            consultation_reference,
            additional_minutes,
            new_consultation_end_time,
        )
        text_content = consultation_extension_confirmation_professional.text(
            display_name,
            client_first_name,
            consultation_reference,
            additional_minutes,
            new_consultation_end_time,
        )

        return EmailService.send_email(to_email, subject, html_content, text_content)
    
    
    
    @staticmethod
    def send_consultation_extension_confirmation_email(
        to_email,
        first_name=None,
        lawyer_name=None,
        additional_minutes=None,
        new_consultation_end_time=None,
        receipt_number=None,
        transaction_id=None,
        payment_date=None,
        subtotal=None,
        vat_amount=None,
        total_paid=None,
    ):
        """
        Send confirmation email to the client after a successful
        consultation extension payment, with a PDF receipt attached
        (automation #8) - the missing method that
        ConsultationExtensionService._send_extension_confirmation_email
        was already calling before this existed.

        Mirrors send_consultation_payment_confirmation_email's shape:
        subtotal/vat_amount/total_paid are RAW GBP amounts (no currency
        symbol) - this method builds the £-prefixed display strings for
        the email body itself, same as that method does, and
        generate_receipt_pdf() prefixes total_paid with currency_symbol
        internally - so total_paid stays RAW when passed into the PDF
        call below, same as #7.

        additional_minutes: caller passes a bare int/str (e.g.
        request_obj.agreed_minutes) - this method appends " minutes" for
        display, since the template just inserts the value as-is.
        """
        from services.email_templates import consultation_extension_confirmation
        from services.receipt_pdf import generate_receipt_pdf, get_currency_symbol

        to_email = (to_email or "").strip().lower()
        first_name = (first_name or "").strip()
        display_name = first_name if first_name else "there"
        lawyer_name = lawyer_name or "your lawyer"

        currency_symbol = get_currency_symbol("GBP")
        subtotal_display = f"{currency_symbol}{subtotal}" if subtotal is not None else "-"
        vat_amount_display = f"{currency_symbol}{vat_amount}" if vat_amount is not None else "-"
        total_paid_display = f"{currency_symbol}{total_paid}" if total_paid is not None else "-"
        additional_minutes_display = f"{additional_minutes} minutes" if additional_minutes is not None else "-"

        subject = "Your consultation has been extended"
        html_content = consultation_extension_confirmation.html(
            display_name,
            lawyer_name,
            additional_minutes_display,
            new_consultation_end_time,
            receipt_number,
            transaction_id,
            payment_date,
            subtotal_display,
            vat_amount_display,
            total_paid_display,
        )
        text_content = consultation_extension_confirmation.text(
            display_name,
            lawyer_name,
            additional_minutes_display,
            new_consultation_end_time,
            receipt_number,
            transaction_id,
            payment_date,
            subtotal_display,
            vat_amount_display,
            total_paid_display,
        )

        receipt_pdf_bytes = generate_receipt_pdf(
            purchase_type=f"Consultation Extension with {lawyer_name}",
            order_id=transaction_id,
            date=payment_date,
            amount_paid=total_paid_display,
            support_contact="support@lawneeds.co.uk",
            total_paid=total_paid,
            currency_symbol=currency_symbol,
        )
        attachments = [
            {
                "filename": f"LawNeeds-Receipt-{receipt_number or 'receipt'}.pdf",
                "content": receipt_pdf_bytes,
                "mimetype": "application/pdf",
            }
        ]

        return EmailService.send_email(
            to_email, subject, html_content, text_content, attachments=attachments
        )
    
