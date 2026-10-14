from fastapi import APIRouter, HTTPException, Request, Body, Query
from openai import OpenAI
import os
import logging
import uuid as _uuid_mod
from typing import Dict, Optional
from pydantic import BaseModel
import json
from fastapi.responses import JSONResponse
import base64
from io import BytesIO

logger = logging.getLogger(__name__)

try:
    from services.subscription_service import SubscriptionService
    from services.subscription_constants import UsageActionType
    SUBSCRIPTION_FEATURES_ENABLED = True
except ImportError:
    SUBSCRIPTION_FEATURES_ENABLED = False

try:
    from services.models import ReferralRequest, LawyerInbox, ReferralSession, ConsultationPayment
    _MODELS_AVAILABLE = True
except ImportError:
    _MODELS_AVAILABLE = False


# Define request and response models
class AudioData(BaseModel):
    audio_data: str
    language: Optional[str] = None  # ISO language code (e.g., 'en', 'es', 'fr')
    referral_id: Optional[str] = None  # When set, indicates audio is for a paid consultation chat

class TranscriptionResponse(BaseModel):
    text: str
    success: str
    detected_language: Optional[str] = None

# Supported languages mapping
SUPPORTED_LANGUAGES = {
            'en': 'English',
            'es': 'Spanish',
            'fr': 'French',
            'de': 'German',
            'it': 'Italian',
            'pt': 'Portuguese',
            'nl': 'Dutch',
            'ja': 'Japanese',
            'ko': 'Korean',
            'zh': 'Chinese',
            'ar': 'Arabic',
            'ru': 'Russian',
            'hi': 'Hindi',
            'tr': 'Turkish',
            'pl': 'Polish',
            'vi': 'Vietnamese',
            'th': 'Thai',
            'ro': 'Romanian',
            'som': 'Somali',
            'tl': 'Tagalog',
            'pa': 'Punjabi',
            'bn': 'Bengali',
            'fa': 'Persian',
            'uk': 'Ukrainian',
            'ku': 'Kurdish',
            'lt': 'Lithuanian',
            'bg': 'Bulgarian',
            'sr': 'Serbian',
            'hu': 'Hungarian',
            'cs': 'Czech',
            'ur': 'Urdu',
            'ne': 'Nepali',
            'sw': 'Swahili',
            'az': 'Azerbaijani',
            'hr': 'Croatian',
            'he': 'Hebrew',
            'sk': 'Slovak',
            'ms': 'Malay',
            'bs': 'Bosnian',
            'ps': 'Pashto',
            'am': 'Amharic',
            'my': 'Burmese',
            'km': 'Khmer',
            'ckb': 'Central Kurdish',
            'si': 'Sinhala',
            'ml': 'Malayalam',
            'ta': 'Tamil'
        }
class SpeechAPI:
    def __init__(self, *, app_name, db, auth):
        self.app_name = app_name
        self.db = db
        self.auth = auth
        self.client = OpenAI(api_key=os.getenv('SECRET_KEY'))
        
    def get_router(self):
        router = APIRouter(prefix="/speech", tags=["speech"])

        @router.get("/languages")
        async def get_supported_languages():
            """Get list of supported languages"""
            return JSONResponse(content=SUPPORTED_LANGUAGES)

        @router.post("/transcribe", response_model=TranscriptionResponse)
        async def transcribe_audio(
            request: Request,
            audio_data: AudioData
        ):
            try:
                # Add debug logging
                logger.info("Starting audio transcription")
                
                # Verify user token
                token_data = self.auth.verify_request_token(request)
                logger.info("User authenticated successfully")
                # Decode base64 audio data
                try:
                    base64_str = audio_data.audio_data
                    if ',' in base64_str:
                        base64_str = base64_str.split(',')[1]
                    audio_bytes = base64.b64decode(base64_str)
                    logger.info(f"Audio decoded successfully, size: {len(audio_bytes)} bytes")
                except Exception as e:
                    logger.error(f"Error decoding base64 audio: {str(e)}")
                    raise HTTPException(status_code=400, detail="Invalid audio data format")
                
                estimated_duration_seconds = len(audio_bytes) / 32000

                # Bypass the freemium voice-note cap when the user has an active,
                # paid consultation session.  We check this in two ways:
                #   1. Explicit: caller passes referral_id — verify that specific case.
                #   2. Auto-detect: query for ANY accepted+paid session belonging to
                #      this user (covers frontends that don't send referral_id).
                is_consultation_recording = False
                if _MODELS_AVAILABLE:
                    try:
                        with self.db.get_session() as session:
                            user_uuid_check = _uuid_mod.UUID(token_data.get('user_id'))

                            if audio_data.referral_id:
                                # --- Explicit referral_id path ---
                                referral = session.query(ReferralRequest).filter(
                                    ReferralRequest.id == _uuid_mod.UUID(audio_data.referral_id),
                                    ReferralRequest.user_id == user_uuid_check,
                                ).first()
                                if referral:
                                    inbox_entry = session.query(LawyerInbox).filter(
                                        LawyerInbox.referral_request_id == referral.id,
                                        LawyerInbox.status == 'accepted',
                                    ).first()
                                    if inbox_entry:
                                        is_consultation_recording = True
                            else:
                                # --- Auto-detect path: any active paid consultation ---
                                paid_session = (
                                    session.query(ReferralSession)
                                    .join(
                                        ConsultationPayment,
                                        ConsultationPayment.session_id == ReferralSession.id,
                                    )
                                    .filter(
                                        ReferralSession.user_id == user_uuid_check,
                                        ReferralSession.session_status.notin_(['completed', 'cancelled']),
                                        ConsultationPayment.payment_status.in_(['paid', 'transferred']),
                                    )
                                    .first()
                                )
                                if paid_session:
                                    is_consultation_recording = True

                            if is_consultation_recording:
                                logger.info(
                                    "Voice note for paid consultation (referral_id=%s) — skipping freemium cap",
                                    audio_data.referral_id or "auto-detected",
                                )
                    except Exception as e:
                        logger.warning("Could not verify consultation status, applying normal limits: %s", e)

                if SUBSCRIPTION_FEATURES_ENABLED and not is_consultation_recording:
                    try:
                        with self.db.get_session() as session:
                            subscription_service = SubscriptionService(session)
                            user_uuid = _uuid_mod.UUID(token_data.get('user_id'))
                            can_do, blocked_reason, message = subscription_service.can_perform_action(
                                user_uuid,
                                UsageActionType.VOICE_NOTE,
                                duration_seconds=int(estimated_duration_seconds)
                            )
                            if not can_do:
                                raise HTTPException(
                                    status_code=429,
                                    detail={
                                        "error": blocked_reason.value if blocked_reason else "voice_note_blocked",
                                        "message": message,
                                        "type": "voice_limit_exceeded"
                                    }
                                )
                    except HTTPException:
                        raise
                    except Exception as e:
                        logger.warning(f"Voice note validation failed, allowing transcription: {e}")
                
                audio_file = BytesIO(audio_bytes)
                audio_file.name = "audio.m4a"
                
                try:
                    logger.info(f"Sending request to Whisper API with language: {audio_data.language}")
                    
                    transcription_params = {
                        "model": "whisper-1",
                        "file": audio_file,
                        "response_format": "verbose_json"
                    }
                    
                    if audio_data.language:
                        transcription_params["language"] = audio_data.language
                    
                    response = self.client.audio.transcriptions.create(**transcription_params)
                    
                    audio_duration = getattr(response, 'duration', estimated_duration_seconds)
                    
                    # Only deduct from the freemium quota when this is NOT a
                    # paid consultation recording.
                    if SUBSCRIPTION_FEATURES_ENABLED and not is_consultation_recording:
                        try:
                            duration_seconds = int(audio_duration or estimated_duration_seconds)
                            duration_seconds = max(duration_seconds, 1)
                            with self.db.get_session() as session:
                                subscription_service = SubscriptionService(session)
                                user_uuid = _uuid_mod.UUID(token_data.get("user_id"))
                                subscription_service.deduct_usage(
                                    user_id=user_uuid,
                                    action_type=UsageActionType.VOICE_NOTE,
                                    duration_seconds=duration_seconds,
                                )
                        except Exception as e:
                            logger.warning(f"Failed to deduct voice transcription usage: {e}")
                    
                    logger.info(
                        "Transcription successful",
                        extra={
                            "detected_language": response.language,
                            "text_length": len(response.text or ""),
                            "audio_duration": audio_duration,
                            "is_consultation_recording": is_consultation_recording,
                        },
                    )
                    return JSONResponse(content={
                        "text": response.text,
                        "success": "true",
                        "detected_language": response.language,
                        "audio_duration": audio_duration
                    })
                    
                except Exception as e:
                    logger.error(f"OpenAI transcription error: {str(e)}")
                    raise HTTPException(status_code=500, detail=f"Transcription failed: {str(e)}")
                
            except HTTPException as he:
                raise he
            except Exception as e:
                logger.error(f"Transcription error: {str(e)}", exc_info=True)
                raise HTTPException(status_code=500, detail=str(e))

        return router
