from openai import OpenAI
import os
import logging
from io import BytesIO
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)

class OpenAIService:
    def __init__(self):
        self.client = OpenAI(api_key=os.getenv('SECRET_KEY'))
    
    async def transcribe_audio(self, audio_bytes: bytes, language: Optional[str] = None) -> Dict[str, Any]:
        """
        Transcribe audio bytes using OpenAI Whisper API
        
        Args:
            audio_bytes: Raw audio data as bytes
            language: Optional language code (e.g., 'en', 'es', 'fr')
            
        Returns:
            Dict containing transcription result
        """
        try:
            # Create a file-like object from bytes
            audio_file = BytesIO(audio_bytes)
            audio_file.name = "audio.wav"  # Whisper API needs a filename
            
            # Prepare transcription parameters
            transcription_params = {
                "model": "whisper-1",
                "file": audio_file,
                "response_format": "verbose_json"  # Get detailed response
            }
            
            # Add language if specified
            if language:
                transcription_params["language"] = language
            
            # Call OpenAI Whisper API
            response = self.client.audio.transcriptions.create(**transcription_params)
            
            logger.info(f"Transcription successful, detected language: {response.language}")
            
            return {
                'success': True,
                'text': response.text,
                'language': response.language,
                'duration': response.duration if hasattr(response, 'duration') else 0
            }
            
        except Exception as e:
            logger.error(f"OpenAI transcription error: {str(e)}")
            return {
                'success': False,
                'text': '',
                'error': str(e)
            }

# Keep the existing client and function for backward compatibility
client = OpenAI(api_key=os.getenv('SECRET_KEY'))

def generate_embedding(text):
    response = client.embeddings.create(
        model="text-embedding-3-small",
        input=text
    )
    return response.data[0].embedding

