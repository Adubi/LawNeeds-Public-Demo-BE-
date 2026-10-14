"""
Firebase Cloud Messaging (FCM) Service for Push Notifications
Handles sending push notifications to iOS and Android devices via Firebase Admin SDK
"""

import logging
import os
from typing import Optional, Dict, List
import firebase_admin
from firebase_admin import credentials, messaging
import json

logger = logging.getLogger(__name__)


class FirebaseNotificationService:
    """Service for sending push notifications via Firebase Cloud Messaging"""
    
    def __init__(self):
        """Initialize Firebase Admin SDK"""
        self.initialized = False
        self._initialize_firebase()
    
    def _initialize_firebase(self):
        """Initialize Firebase Admin SDK with credentials"""
        try:
            # Check if Firebase is already initialized
            if firebase_admin._apps:
                logger.info("Firebase Admin SDK already initialized")
                self.initialized = True
                return
            
            # Try to get credentials from environment variable (JSON string)
            firebase_creds_json = os.getenv('FIREBASE_CREDENTIALS_JSON')
            
            if firebase_creds_json:
                # Parse JSON credentials from environment variable
                creds_dict = json.loads(firebase_creds_json)
                cred = credentials.Certificate(creds_dict)
                firebase_admin.initialize_app(cred)
                logger.info("Firebase Admin SDK initialized successfully from environment variable")
                self.initialized = True
                return
            
            # Try to get credentials from file path
            firebase_creds_path = os.getenv('FIREBASE_CREDENTIALS_PATH', 'firebase-service-account.json')
            
            if os.path.exists(firebase_creds_path):
                cred = credentials.Certificate(firebase_creds_path)
                firebase_admin.initialize_app(cred)
                logger.info(f"Firebase Admin SDK initialized successfully from file: {firebase_creds_path}")
                self.initialized = True
                return
            
            # If no credentials found, log warning but don't fail
            logger.warning(
                "Firebase credentials not found. Push notifications will not be sent. "
                "Set FIREBASE_CREDENTIALS_JSON environment variable or place firebase-service-account.json in project root."
            )
            self.initialized = False
            
        except Exception as e:
            logger.error(f"Failed to initialize Firebase Admin SDK: {e}")
            self.initialized = False
    
    async def send_notification(
        self,
        device_token: str,
        title: str,
        body: str,
        data: Optional[Dict] = None,
        badge: Optional[int] = None,
        sound: str = 'default',
        channel_id: str = 'high_importance_channel'
    ) -> bool:
        """
        Send a push notification to a single device.

        Configured for **guaranteed delivery to background and killed apps**:
          • iOS: apns-push-type=alert + ApsAlert payload + apns-priority=10 +
            mutable-content=1. Required by iOS 13+; without these headers the
            push is dropped or treated as silent and the system tray
            notification never appears when the app is killed/in another tab.
          • Android: priority=high + high_importance_channel — wakes the device
            and triggers the notification even when the app is in background.
            (When the app has been *force-stopped* on Android, the OS itself
            blocks all FCMs until the user reopens the app — that is an
            Android platform restriction we cannot work around server-side.)

        Args:
            device_token: FCM device token
            title: Notification title
            body: Notification body text
            data: Custom data payload (dict)
            badge: Badge count for iOS (optional)
            sound: Sound to play (default: 'default')
            channel_id: Android notification channel ID

        Returns:
            bool: True if successful, False otherwise
        """
        if not self.initialized:
            logger.warning(f"Firebase not initialized. Cannot send notification: {title}")
            return False

        try:
            # Prepare data payload (all values must be strings for FCM).
            # "priority": "high" is included at the root of the data map so that
            # receivers can read it; the actual FCM delivery priority is controlled
            # by AndroidConfig.priority below (maps to android.priority = "high"
            # in the FCM v1 HTTP payload).
            notification_data: Dict[str, str] = {'priority': 'high'}
            if data:
                for key, value in data.items():
                    notification_data[key] = str(value) if not isinstance(value, str) else value

            message = messaging.Message(
                notification=messaging.Notification(
                    title=title,
                    body=body,
                ),
                data=notification_data,
                token=device_token,
                android=messaging.AndroidConfig(
                    priority='high',
                    notification=messaging.AndroidNotification(
                        channel_id=channel_id,
                        sound=sound,
                        default_sound=True,
                        default_vibrate_timings=True,
                    ),
                ),
                apns=messaging.APNSConfig(
                    payload=messaging.APNSPayload(
                        aps=messaging.Aps(
                            # ApsAlert ensures the alert renders even when the
                            # app is killed; relying on the top-level
                            # `notification` block alone is unreliable on iOS 13+.
                            alert=messaging.ApsAlert(title=title, body=body),
                            sound=sound,
                            badge=badge,
                            mutable_content=True,
                        ),
                    ),
                    headers={
                        # Required by iOS 13+ for visible alerts. Without it
                        # APNs may reject or downgrade the push, so users on
                        # backgrounded/killed apps never see it.
                        'apns-push-type': 'alert',
                        'apns-priority': '10',
                    },
                ),
            )

            response = messaging.send(message)
            logger.info(f"Successfully sent notification to device: {response}")
            return True

        except messaging.UnregisteredError:
            logger.warning(f"Device token is unregistered or invalid: {device_token}")
            return False
        except Exception as e:
            logger.error(f"Error sending notification: {e}")
            return False
    
    async def send_notification_to_multiple(
        self,
        device_tokens: List[str],
        title: str,
        body: str,
        data: Optional[Dict] = None,
        badge: Optional[int] = None,
        sound: str = 'default',
        channel_id: str = 'high_importance_channel'
    ) -> Dict[str, int]:
        """
        Send push notification to multiple devices
        
        Args:
            device_tokens: List of FCM device tokens
            title: Notification title
            body: Notification body text
            data: Custom data payload (dict)
            badge: Badge count for iOS (optional)
            sound: Sound to play (default: 'default')
            channel_id: Android notification channel ID
            
        Returns:
            Dict with 'success_count' and 'failure_count'
        """
        if not self.initialized:
            logger.warning(f"Firebase not initialized. Cannot send notifications: {title}")
            return {'success_count': 0, 'failure_count': len(device_tokens)}
        
        success_count = 0
        failure_count = 0
        
        for token in device_tokens:
            result = await self.send_notification(
                device_token=token,
                title=title,
                body=body,
                data=data,
                badge=badge,
                sound=sound,
                channel_id=channel_id
            )
            if result:
                success_count += 1
            else:
                failure_count += 1
        
        logger.info(f"Batch notification sent. Success: {success_count}, Failed: {failure_count}")
        return {
            'success_count': success_count,
            'failure_count': failure_count,
            'total': len(device_tokens)
        }
    
    async def send_new_referral_notification(
        self,
        device_tokens: List[str],
        client_name: str,
        referral_id: str,
        user_id: str,
        legal_area: str = None
    ) -> Dict[str, int]:
        """
        Send notification to lawyer when they receive a new referral request
        
        Args:
            device_tokens: Lawyer's device tokens
            client_name: Name of the client
            referral_id: ID of the referral request
            user_id: ID of the user who made the request
            legal_area: Legal area of the request
            
        Returns:
            Dict with success/failure counts
        """
        title = "New Client Request! 📋"
        body = f"{client_name} has sent you a referral request"
        if legal_area:
            body += f" for {legal_area}"
        
        data = {
            'type': 'new_referral',
            'referral_id': referral_id,
            'user_id': user_id,
            'screen': 'LawyerInbox',  # Navigate to lawyer inbox
            'action': 'view_referral'
        }
        
        
        
        return await self.send_notification_to_multiple(
            device_tokens=device_tokens,
            title=title,
            body=body,
            data=data,
            badge=1
        )

    async def send_missed_consultation_notification(
        self,
        device_tokens: List[str],
        session_id: str,
        referral_id: str
    ) -> Dict[str, int]:
        """
        Send notification to lawyer when a scheduled consultation is
        missed (never started within the grace period)

        Args:
            device_tokens: Lawyer's device tokens
            session_id: ID of the consultation session
            referral_id: ID of the referral request

        Returns:
            Dict with success/failure counts
        """
        title = "Missed Consultation"
        body = "A scheduled consultation was missed and never started."

        data = {
            'type': 'missed_consultation',
            'session_id': session_id,
            'referral_id': referral_id,
            'screen': 'ConsultationScreen',
            'action': 'view_status'
        }

        return await self.send_notification_to_multiple(
            device_tokens=device_tokens,
            title=title,
            body=body,
            data=data,
            badge=1
        )

    async def send_response_rate_warning_notification(
        self,
        device_tokens: List[str],
        response_rate: str
    ) -> Dict[str, int]:
        """
        Send notification to lawyer when their rolling 90-day referral
        response rate drops below the warning threshold

        Args:
            device_tokens: Lawyer's device tokens
            response_rate: Formatted rate string, e.g. '42%'

        Returns:
            Dict with success/failure counts
        """
        title = "Response Rate Below Target"
        body = f"Your referral response rate is {response_rate} over the last 90 days."

        data = {
            'type': 'response_rate_warning',
            'response_rate': response_rate,
            'screen': 'Profile',
            'action': 'view_status'
        }

        return await self.send_notification_to_multiple(
            device_tokens=device_tokens,
            title=title,
            body=body,
            data=data,
            badge=1
        )
        
    async def send_new_review_notification(
        self,
        device_tokens: List[str],
        reviewer_name: str,
        rating: int,
        review_id: str
    ) -> Dict[str, int]:
        """
        Send notification to lawyer when they receive a new review

        Args:
            device_tokens: Lawyer's device tokens
            reviewer_name: Name of the reviewer
            rating: Star rating (1-5)
            review_id: ID of the review

        Returns:
            Dict with success/failure counts
        """
        title = "You have a new review"
        body = f"{reviewer_name} left you a {rating}-star review"

        data = {
            'type': 'new_review',
            'review_id': review_id,
            'rating': str(rating),
            'screen': 'LawyerReviewsDashboard',
            'action': 'view_review'
        }

        return await self.send_notification_to_multiple(
            device_tokens=device_tokens,
            title=title,
            body=body,
            data=data,
            badge=1
        )
    async def send_payout_sent_notification(
        self,
        device_tokens: List[str],
        amount_gbp: str,
        session_id: str,
        referral_id: str
    ) -> Dict[str, int]:
        """
        Send notification to lawyer when their consultation payout is sent

        Args:
            device_tokens: Lawyer's device tokens
            amount_gbp: Payout amount as a GBP string, e.g. '120.00'
            session_id: ID of the consultation session
            referral_id: ID of the referral request

        Returns:
            Dict with success/failure counts
        """
        title = "Payout Sent"
        body = f"Your payout of GBP {amount_gbp} has been sent"

        data = {
            'type': 'payout_sent',
            'session_id': session_id,
            'referral_id': referral_id,
            'screen': 'ConsultationSummary',
            'action': 'view_payout'
        }

        return await self.send_notification_to_multiple(
            device_tokens=device_tokens,
            title=title,
            body=body,
            data=data,
            badge=1
        )

    async def send_payout_failed_notification(
        self,
        device_tokens: List[str],
        reason: str,
        session_id: str,
        referral_id: str
    ) -> Dict[str, int]:
        """
        Send notification to lawyer when their consultation payout fails

        Args:
            device_tokens: Lawyer's device tokens
            reason: Short human-readable failure reason
            session_id: ID of the consultation session
            referral_id: ID of the referral request

        Returns:
            Dict with success/failure counts
        """
        title = "Payout Failed"
        body = f"We could not send your payout: {reason}"

        data = {
            'type': 'payout_failed',
            'session_id': session_id,
            'referral_id': referral_id,
            'screen': 'ConsultationSummary',
            'action': 'view_payout'
        }

        return await self.send_notification_to_multiple(
            device_tokens=device_tokens,
            title=title,
            body=body,
            data=data,
            badge=1
        )

    async def send_referral_accepted_notification(
        self,
        device_tokens: List[str],
        lawyer_name: str,
        referral_id: str,
        session_id: str,
        lawyer_id: str
    ) -> Dict[str, int]:
        """
        Send notification to user when lawyer accepts their referral request
        
        Args:
            device_tokens: User's device tokens
            lawyer_name: Name of the lawyer
            referral_id: ID of the referral request
            session_id: ID of the created session
            lawyer_id: ID of the lawyer
            
        Returns:
            Dict with success/failure counts
        """
        title = "Lawyer Accepted Your Request! 🎉"
        body = f"{lawyer_name} has accepted your case and is ready to assist you."
        
        data = {
            'type': 'referral_accepted',
            'referral_id': referral_id,
            'session_id': session_id,
            'lawyer_id': lawyer_id,
            'screen': 'InboxConversation',  # Navigate to inbox conversation
            'action': 'view_conversation'
        }
        
        return await self.send_notification_to_multiple(
            device_tokens=device_tokens,
            title=title,
            body=body,
            data=data,
            badge=1
        )
    
    async def send_consultation_booked_notification(
        self,
        device_tokens: List[str],
        lawyer_name: str,
        referral_id: str,
        session_id: str,
        requested_start_at_iso: str,
    ) -> Dict[str, int]:
        """
        Send notification to user when their consultation booking is
        confirmed with a specific date/time, alongside the existing
        generic referral-accepted notification.

        Args:
            device_tokens: User's device tokens
            lawyer_name: Name of the lawyer
            referral_id: ID of the referral request
            session_id: ID of the created session
            requested_start_at_iso: ISO-8601 timestamp of the booked start time

        Returns:
            Dict with success/failure counts
        """
        title = "Consultation Booked"
        body = f"Your consultation with {lawyer_name} is confirmed."

        data = {
            'type': 'consultation_booked',
            'referral_id': referral_id,
            'session_id': session_id,
            'requested_start_at': requested_start_at_iso,
            'screen': 'ConsultationScreen',
            'action': 'view_consultation'
        }

        return await self.send_notification_to_multiple(
            device_tokens=device_tokens,
            title=title,
            body=body,
            data=data,
            badge=1
        )
    async def send_referral_rejected_notification(
        self,
        device_tokens: List[str],
        lawyer_name: str,
        referral_id: str,
        reason: str = None
    ) -> Dict[str, int]:
        """
        Send notification to user when lawyer rejects their referral request
        
        Args:
            device_tokens: User's device tokens
            lawyer_name: Name of the lawyer
            referral_id: ID of the referral request
            reason: Rejection reason (optional)
            
        Returns:
            Dict with success/failure counts
        """
        title = "Referral Update"
        body = f"{lawyer_name} is unable to take your case at this time."
        if reason:
            body += f" Reason: {reason}"
        
        data = {
            'type': 'referral_rejected',
            'referral_id': referral_id,
            'screen': 'Referrals',
            'action': 'view_status'
        }
        
        return await self.send_notification_to_multiple(
            device_tokens=device_tokens,
            title=title,
            body=body,
            data=data
        )

    async def send_referral_expired_notification(
        self,
        device_tokens: List[str],
        referral_id: str
    ) -> Dict[str, int]:
        """
        Send notification when a pending referral expires without a lawyer response.

        Args:
            device_tokens: Recipient's device tokens (called for both the
                user and the lawyer)
            referral_id: ID of the referral request

        Returns:
            Dict with success/failure counts
        """
        title = "Referral Expired"
        body = "This referral expired without a response and is no longer active."

        data = {
            'type': 'referral_expired',
            'referral_id': referral_id,
            'screen': 'Referrals',
            'action': 'view_status'
        }

        return await self.send_notification_to_multiple(
            device_tokens=device_tokens,
            title=title,
            body=body,
            data=data
        )

    async def send_case_cancelled_notification(
        self,
        device_tokens: List[str],
        client_name: str,
        referral_id: str,
        reason: str = None,
        is_deletion: bool = False
    ) -> Dict[str, int]:
        """
        Send notification to lawyer when user cancels or deletes their case request.

        Args:
            device_tokens: Lawyer's device tokens
            client_name: Name of the client (or 'A client' for privacy)
            referral_id: ID of the referral request
            reason: Cancellation reason (optional)
            is_deletion: True if this is a deletion, False if withdrawal/cancel

        Returns:
            Dict with success/failure counts
        """
        if is_deletion:
            title = "Case Request Removed"
            body = f"{client_name} has removed their case request from the system."
        else:
            title = "Case Request Withdrawn"
            body = f"{client_name} has withdrawn their case request."
        
        if reason:
            body += f" Reason: {reason}"

        data = {
            'type': 'case_cancelled_by_user',
            'referral_id': referral_id,
            'is_deletion': str(is_deletion).lower(),
            'screen': 'LawyerInbox',
            'action': 'refresh_inbox'
        }

        return await self.send_notification_to_multiple(
            device_tokens=device_tokens,
            title=title,
            body=body,
            data=data,
            badge=1
        )
    
    async def send_price_proposed_notification(
        self,
        device_tokens: List[str],
        lawyer_name: str,
        session_id: str,
        referral_id: str,
        amount_display: str,
    ) -> Dict[str, int]:
        """
        Notify the user when a lawyer proposes a consultation fee.

        Args:
            device_tokens: User's device tokens
            lawyer_name: Name of the lawyer
            session_id: ID of the consultation session
            referral_id: ID of the referral request
            amount_display: Human-readable amount e.g. '£150'
        """
        title = "Your Lawyer Has Sent a Quote 💷"
        body = f"{lawyer_name} has proposed a fee of {amount_display} for your consultation. Review and confirm to proceed."

        data = {
            'type': 'price_proposed',
            'session_id': session_id,
            'referral_id': referral_id,
            'screen': 'ConsultationPayment',
            'action': 'review_price',
        }

        return await self.send_notification_to_multiple(
            device_tokens=device_tokens,
            title=title,
            body=body,
            data=data,
            badge=1,
        )

    async def send_payment_confirmed_notification(
        self,
        device_tokens: List[str],
        user_name: str,
        session_id: str,
        referral_id: str,
    ) -> Dict[str, int]:
        """
        Notify the lawyer when the user has successfully paid for a consultation.

        Args:
            device_tokens: Lawyer's device tokens
            user_name: Name of the user (client)
            session_id: ID of the consultation session
            referral_id: ID of the referral request
        """
        title = "Payment Received! 💰"
        body = f"{user_name} has paid for the consultation. You're all set to begin."

        data = {
            'type': 'payment_confirmed',
            'session_id': session_id,
            'referral_id': referral_id,
            'screen': 'ConsultationSession',
            'action': 'start_consultation',
        }

        return await self.send_notification_to_multiple(
            device_tokens=device_tokens,
            title=title,
            body=body,
            data=data,
            badge=1,
        )

    async def send_session_ended_notification(
        self,
        device_tokens: List[str],
        other_party_name: str,
        session_id: str,
        referral_id: str,
        recipient_type: str,
    ) -> Dict[str, int]:
        """
        Notify both the user and lawyer that a consultation session has ended.

        Uses maximum-priority FCM settings so the notification is delivered
        regardless of whether the recipient's app is in the foreground, background,
        or has been killed entirely.

        Args:
            device_tokens: Recipient's device tokens
            other_party_name: Name of the other participant
            session_id: ID of the consultation session
            referral_id: ID of the referral request
            recipient_type: 'user' or 'lawyer'
        """
        if recipient_type == 'lawyer':
            title = "Consultation Session Ended"
            body = f"Your session with {other_party_name} has now ended. Thank you for your service."
        else:
            title = "Consultation Session Ended"
            body = f"Your session with {other_party_name} has now ended. We hope it was helpful!"

        data = {
            'type': 'session_ended',
            'session_id': session_id,
            'referral_id': referral_id,
            'screen': 'ConsultationSummary',
            'action': 'view_summary',
        }

        return await self.send_notification_to_multiple(
            device_tokens=device_tokens,
            title=title,
            body=body,
            data=data,
            badge=1,
            sound='default',
            channel_id='high_importance_channel',
        )

    async def send_day_completed_notification(
        self,
        device_tokens: List[str],
        other_party_name: str,
        day_number: int,
        days_total: int,
        remaining_days: int,
        session_id: str,
        referral_id: str,
    ) -> Dict[str, int]:
        """Notify both parties that a daily session slot has ended (non-final day).

        Args:
            device_tokens: Recipient's device tokens
            other_party_name: Display name of the other participant
            day_number: Which day just ended (1-based)
            days_total: Total number of days in the session
            remaining_days: Number of days still remaining
            session_id: ID of the consultation session
            referral_id: ID of the referral request
        """
        title = f"Day {day_number} of {days_total} Completed"
        body = (
            f"Your Day {day_number} session with {other_party_name} has ended. "
            f"{remaining_days} day{'s' if remaining_days != 1 else ''} remaining."
        )
        data = {
            'type': 'day_completed',
            'session_id': session_id,
            'referral_id': referral_id,
            'day_number': str(day_number),
            'days_total': str(days_total),
            'remaining_days': str(remaining_days),
            'screen': 'ConsultationSummary',
            'action': 'view_summary',
        }
        return await self.send_notification_to_multiple(
            device_tokens=device_tokens,
            title=title,
            body=body,
            data=data,
            badge=1,
            sound='default',
            channel_id='high_importance_channel',
        )

    async def send_one_minute_warning_notification(
        self,
        device_tokens: List[str],
        session_id: str,
        referral_id: str,
    ) -> Dict[str, int]:
        """
        Notify both user and lawyer that 1 minute remains in the consultation.

        Args:
            device_tokens: Recipient's device tokens
            session_id: ID of the consultation session
            referral_id: ID of the referral request
        """
        title = "⏰ 1 Minute Remaining"
        body = "Your consultation session will end in 1 minute."

        data = {
            'type': 'one_minute_warning',
            'session_id': session_id,
            'referral_id': referral_id,
            'screen': 'ConsultationSession',
            'action': 'view_session',
        }

        return await self.send_notification_to_multiple(
            device_tokens=device_tokens,
            title=title,
            body=body,
            data=data,
        )

    async def send_consultation_extended_notification(
        self,
        device_tokens: List[str],
        user_name: str,
        session_id: str,
        referral_id: str,
        extended_by_minutes: int,
        new_end_time_iso: str,
    ) -> Dict[str, int]:
        """Notify the lawyer when a user extends consultation time."""
        title = "Consultation Extended"
        body = f"{user_name} extended the consultation by {extended_by_minutes} minutes."

        data = {
            'type': 'consultation_extended',
            'session_id': session_id,
            'referral_id': referral_id,
            'extended_by_minutes': str(extended_by_minutes),
            'new_end_time': new_end_time_iso,
            'screen': 'ConsultationSession',
            'action': 'view_session',
        }

        return await self.send_notification_to_multiple(
            device_tokens=device_tokens,
            title=title,
            body=body,
            data=data,
            badge=1,
            sound='default',
            channel_id='high_importance_channel',
        )

    async def send_pending_action_notification(
        self,
        device_tokens: List[str],
        professional_name: str,
        booking_request_id: str,
        referral_id: str,
    ) -> Dict[str, int]:
        """
        Notify a user that their pending casework consultation booking still
        needs action. Fires at the 24h and 72h reminder stages inside
        send_consultation_booking_reminders(), alongside the existing
        EmailService.send_pending_action_email() call -- this is the push
        equivalent of that email, sharing the same reminder_24h_sent_at /
        reminder_72h_sent_at dedup columns so push and email fire together.

        Args:
            device_tokens: User's device tokens
            professional_name: Name of the professional awaiting a response
            booking_request_id: ID of the ConsultationBookingRequest
            referral_id: ID of the linked referral request
        """
        title = "Action Needed on Your Booking"
        body = f"Your consultation request with {professional_name} is still pending. Act now to keep things on track."

        data = {
            'type': 'continue_journey',
            'booking_request_id': booking_request_id,
            'referral_id': referral_id,
            'screen': 'BookingStatus',
            'action': 'view_booking',
        }

        return await self.send_notification_to_multiple(
            device_tokens=device_tokens,
            title=title,
            body=body,
            data=data,
            badge=1,
        )

    async def send_new_message_notification(
        self,
        device_tokens: List[str],
        sender_name: str,
        message_preview: str,
        referral_id: str,
        sender_id: str,
        sender_type: str,
        session_id: Optional[str] = None
    ) -> Dict[str, int]:
        """
        Send notification for new inbox message
        
        Args:
            device_tokens: Recipient's device tokens
            sender_name: Name of the sender
            message_preview: Preview of the message (first 100 chars)
            referral_id: ID of the referral (conversation)
            sender_id: ID of the sender
            sender_type: 'user' or 'lawyer'
            session_id: ID of the consultation session (required to fetch session data on consultation page)
            
        Returns:
            Dict with success/failure counts
        """
        title = f"New message from {sender_name}"
        body = message_preview[:100] + "..." if len(message_preview) > 100 else message_preview
        
        data = {
            'type': 'new_message',
            'referral_id': referral_id,
            'sender_id': sender_id,
            'sender_type': sender_type,
            'screen': 'InboxConversation',  # Navigate to conversation
            'action': 'view_message'
        }
        
        if session_id:
            data['session_id'] = session_id
        
        return await self.send_notification_to_multiple(
            device_tokens=device_tokens,
            title=title,
            body=body,
            data=data,
            badge=1
        )

    async def send_welcome_back_notification(
        self,
        device_tokens: List[str],
        user_name: str = None,
    ) -> Dict[str, int]:
        """
        Notify a user who has been inactive for 14+ days, encouraging them
        to return to the app.

        Args:
            device_tokens: User's device tokens
            user_name: Name of the user (optional, for personalization)

        Returns:
            Dict with success/failure counts
        """
        title = "We Miss You! 👋"
        if user_name:
            body = f"Hi {user_name}, it's been a while — come see what's new."
        else:
            body = "It's been a while — come see what's new."

        data = {
            'type': 'welcome_back',
            'screen': 'Home',
            'action': 'open_app',
        }

        return await self.send_notification_to_multiple(
            device_tokens=device_tokens,
            title=title,
            body=body,
            data=data,
        )

    async def send_app_update_notification(
        self,
        device_tokens: List[str],
        latest_version: str,
    ) -> Dict[str, int]:
        """
        Notify a user that a newer version of the app is available, based on
        comparing DeviceToken.app_version against the stored latest-version
        config value for their platform (see AppSetting rows
        latest_ios_version / latest_android_version).

        Args:
            device_tokens: Device tokens for devices below latest_version
            latest_version: The latest available app version, e.g. 2.5.0
        """
        title = "Update Available"
        body = f"A new version of LawNeeds (v{latest_version}) is available. Update now for the latest features and fixes."

        data = {
            'type': 'app_update_available',
            'latest_version': latest_version,
            'screen': 'AppUpdate',
            'action': 'open_store',
        }

        return await self.send_notification_to_multiple(
            device_tokens=device_tokens,
            title=title,
            body=body,
            data=data,
            badge=1,
        )


    async def send_identity_verification_completed_notification(
        self,
        device_tokens: List[str],
        user_id: str,
    ) -> Dict[str, int]:
        """
        Notify a user/lawyer that their identity verification has been approved.

        Args:
            device_tokens: Recipient's device tokens
            user_id: ID of the verified user
        """
        title = "Identity Verified"
        body = "Your identity has been successfully verified."

        data = {
            'type': 'verification_completed',
            'user_id': user_id,
            'screen': 'identity-verification',
            'action': 'view_verification',
        }

        return await self.send_notification_to_multiple(
            device_tokens=device_tokens,
            title=title,
            body=body,
            data=data,
            badge=1,
        )

    async def send_identity_verification_resubmission_notification(
        self,
        device_tokens: List[str],
        user_id: str,
    ) -> Dict[str, int]:
        """
        Notify a user/lawyer that their identity verification was declined/failed
        and needs to be resubmitted.

        Args:
            device_tokens: Recipient's device tokens
            user_id: ID of the affected user
        """
        title = "Action Needed: Identity Verification"
        body = "We weren't able to verify your identity. Please resubmit your documents."

        data = {
            'type': 'verification_resubmission_required',
            'user_id': user_id,
            'screen': 'identity-verification',
            'action': 'resubmit_verification',
        }

        return await self.send_notification_to_multiple(
            device_tokens=device_tokens,
            title=title,
            body=body,
            data=data,
            badge=1,
        )


    async def send_identity_verification_not_commenced_notification(
        self,
        device_tokens: List[str],
        user_id: str,
    ) -> Dict[str, int]:
        """
        Notify a user/lawyer who has not started identity verification yet.

        Args:
            device_tokens: Recipient's device tokens
            user_id: ID of the user
        """
        title = "Verify Your Identity"
        body = "You haven't started identity verification yet. It only takes a few minutes."

        data = {
            'type': 'verification_not_commenced',
            'user_id': user_id,
            'screen': 'identity-verification',
            'action': 'start_verification',
        }

        return await self.send_notification_to_multiple(
            device_tokens=device_tokens,
            title=title,
            body=body,
            data=data,
            badge=1,
        )


    async def send_identity_verification_abandoned_notification(
        self,
        device_tokens: List[str],
        user_id: str,
    ) -> Dict[str, int]:
        """
        Notify a user/lawyer whose identity verification has been stuck
        in_progress for too long with no activity.

        Args:
            device_tokens: Recipient's device tokens
            user_id: ID of the user
        """
        title = "Finish Verifying Your Identity"
        body = "Your identity verification is still incomplete. Pick up where you left off."

        data = {
            'type': 'verification_abandoned',
            'user_id': user_id,
            'screen': 'identity-verification',
            'action': 'resume_verification',
        }

        return await self.send_notification_to_multiple(
            device_tokens=device_tokens,
            title=title,
            body=body,
            data=data,
            badge=1,
        )

    async def send_profile_incomplete_notification(
        self,
        device_tokens: List[str],
        user_id: str,
    ) -> Dict[str, int]:
        """
        Notify a user that their profile is missing required fields.

        Args:
            device_tokens: User's device tokens
            user_id: ID of the user, so the client can route to their profile

        Returns:
            Dict with success/failure counts
        """
        title = "Complete Your Profile"
        body = "A few details are still missing from your profile. Finish it to get the most out of LawNeeds."

        data = {
            'type': 'profile_incomplete',
            'user_id': user_id,
            'screen': 'ProfileEdit',
            'action': 'complete_profile',
        }

        return await self.send_notification_to_multiple(
            device_tokens=device_tokens,
            title=title,
            body=body,
            data=data,
            badge=1,
        )

    async def send_subscription_expires_today_notification(
        self,
        device_tokens: List[str],
        plan_name: str,
        subscription_id: str,
    ) -> Dict[str, int]:
        """Notify a user their subscription renews or expires today."""
        title = "Your Subscription Expires Today"
        body = f"Your {plan_name} plan expires today. Renew now to avoid interruption."

        data = {
            "type": "subscription_expires_today",
            "subscription_id": subscription_id,
            "plan_name": plan_name,
            "screen": "current-plan-screen",
            "action": "view_subscription",
        }

        return await self.send_notification_to_multiple(
            device_tokens=device_tokens,
            title=title,
            body=body,
            data=data,
            badge=1,
        )

    async def send_subscription_expired_notification(
        self,
        device_tokens: List[str],
        plan_name: str,
        subscription_id: str,
    ) -> Dict[str, int]:
        """Notify a user their subscription has expired and they have been downgraded."""
        title = "Your Subscription Has Expired"
        body = f"Your {plan_name} plan has expired and your account has been moved to the free plan."

        data = {
            "type": "subscription_expired",
            "subscription_id": subscription_id,
            "plan_name": plan_name,
            "screen": "subscription-plans-screen",
            "action": "renew_subscription",
        }

        return await self.send_notification_to_multiple(
            device_tokens=device_tokens,
            title=title,
            body=body,
            data=data,
            badge=1,
        )

    async def send_subscription_renewed_notification(
        self,
        device_tokens: List[str],
        plan_name: str,
        subscription_id: str,
    ) -> Dict[str, int]:
        """Notify a user their subscription has successfully renewed."""
        title = "Subscription Renewed"
        body = f"Your {plan_name} plan has been renewed successfully."

        data = {
            "type": "subscription_renewed",
            "subscription_id": subscription_id,
            "plan_name": plan_name,
            "screen": "manageSubscription/current-plan-screen",
            "action": "view_subscription",
        }

        return await self.send_notification_to_multiple(
            device_tokens=device_tokens,
            title=title,
            body=body,
            data=data,
            badge=1,
        )

    async def send_call_requested_notification(
        self,
        device_tokens: List[str],
        user_name: str,
        call_type: str,
        call_id: str,
        session_id: str,
        referral_id: str,
    ) -> Dict[str, int]:
        """Notify the lawyer that a user has requested a voice/video call."""
        title = "Call Request"
        body = f"{user_name} has requested a {call_type} call."

        data = {
            "type": "call_requested",
            "call_id": call_id,
            "session_id": session_id,
            "referral_id": referral_id,
            "call_type": call_type,
            "screen": "IncomingCallScreen",
            "action": "view_call_request",
        }

        return await self.send_notification_to_multiple(
            device_tokens=device_tokens,
            title=title,
            body=body,
            data=data,
            badge=1,
        )
    
    
   
    async def send_call_accepted_notification(
        self,
        device_tokens: List[str],
        call_type: str,
        call_id: str,
        session_id: str,
    ) -> Dict[str, int]:
        """Notify the original caller that the other party accepted the call."""
        title = "Call Accepted"
        body = f"Your {call_type} call was accepted."

        data = {
            "type": "call_accepted",
            "call_id": call_id,
            "session_id": session_id,
            "call_type": call_type,
            "screen": "ConsultationScreen",
            "action": "join_call",
        }

        return await self.send_notification_to_multiple(
            device_tokens=device_tokens,
            title=title,
            body=body,
            data=data,
            badge=1,
        )
   
    async def send_call_declined_notification(
        self,
        device_tokens: List[str],
        call_type: str,
        call_id: str,
        session_id: str,
    ) -> Dict[str, int]:
        """Notify the original caller that the other party declined the call."""
        title = "Call Declined"
        body = f"Your {call_type} call was declined."

        data = {
            "type": "call_declined",
            "call_id": call_id,
            "session_id": session_id,
            "call_type": call_type,
            "screen": "RequestDetails",
            "action": "call_declined",
        }

        return await self.send_notification_to_multiple(
            device_tokens=device_tokens,
            title=title,
            body=body,
            data=data,
            badge=1,
        )
    
    
    
    
    async def send_incoming_call_notification(
        self,
        device_tokens: List[str],
        caller_name: str,
        call_type: str,
        call_id: str,
        session_id: str,
        referral_id: str,
    ) -> Dict[str, int]:
        """Notify the user of an incoming lawyer-started voice/video call."""
        title = f"Incoming LawNeeds {call_type} call"
        body = f"Incoming {call_type} call from {caller_name}."

        data = {
            "type": "incoming_call",
            "call_id": call_id,
            "session_id": session_id,
            "referral_id": referral_id,
            "call_type": call_type,
            "screen": "IncomingCallScreen",
            "action": "answer_call",
        }

        return await self.send_notification_to_multiple(
            device_tokens=device_tokens,
            title=title,
            body=body,
            data=data,
            badge=1,
        )
    async def send_missed_call_notification(
        self,
        device_tokens: List[str],
        call_type: str,
        call_id: str,
        session_id: str,
    ) -> Dict[str, int]:
        """Notify a user they missed a voice/video call."""
        title = "Missed Call"
        body = f"You missed a {call_type} call."

        data = {
            "type": "missed_call",
            "call_id": call_id,
            "session_id": session_id,
            "call_type": call_type,
            "screen": "ConsultationScreen",
            "action": "view_call_status",
        }

        return await self.send_notification_to_multiple(
            device_tokens=device_tokens,
            title=title,
            body=body,
            data=data,
            badge=1,
        )