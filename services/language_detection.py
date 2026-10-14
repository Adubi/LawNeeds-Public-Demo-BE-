import logging
import os
from typing import Optional, Dict
from langdetect import detect as langdetect_detect, detect_langs
from langdetect.lang_detect_exception import LangDetectException
from openai import OpenAI

# Import configuration
try:
    from config.language_detection_config import LANGUAGE_DETECTION_CONFIG
except ImportError:
    # Fallback if config not available
    LANGUAGE_DETECTION_CONFIG = {
        'method': 'hybrid',
        'confidence_threshold': 0.7,
        'use_gpt': True,
        'gpt_min_length': 20,
        'gpt_model': 'gpt-3.5-turbo',
        'langdetect_attempts': 3,
        'debug_logging': False,
    }

logger = logging.getLogger(__name__)

# Map of language codes to language names
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
    'ta': 'Tamil',
    'el': 'Greek',
    'sq': 'Albanian',
    'lv': 'Latvian',
}

# Initialize OpenAI client
_openai_client = None

def get_openai_client():
    """Get or create OpenAI client"""
    global _openai_client
    if _openai_client is None:
        api_key = os.getenv('SECRET_KEY') or os.getenv('OPENAI_API_KEY')
        if api_key:
            _openai_client = OpenAI(api_key=api_key)
    return _openai_client

def langdetect_with_confidence(text: str) -> Dict[str, any]:
    """
    Use langdetect with confidence scoring and multiple attempts
    """
    try:
        if not text or len(text.strip()) < 10:
            return {'language': 'en', 'confidence': 0.5}
        
        # Run detection multiple times for consistency
        detections = []
        attempts = LANGUAGE_DETECTION_CONFIG.get('langdetect_attempts', 3)
        
        for _ in range(attempts):
            try:
                # Get probabilities for this attempt
                lang_probs = detect_langs(text)
                if lang_probs:
                    # Store the top detection
                    detections.append({
                        'lang': lang_probs[0].lang,
                        'prob': lang_probs[0].prob
                    })
            except:
                # Try simple detection as fallback
                try:
                    detected = langdetect_detect(text)
                    detections.append({
                        'lang': detected,
                        'prob': 0.7
                    })
                except:
                    pass
        
        if not detections:
            return {'language': 'en', 'confidence': 0.3}
        
        # Count occurrences of each language
        lang_counts = {}
        for det in detections:
            lang = det['lang']
            if lang not in lang_counts:
                lang_counts[lang] = {'count': 0, 'max_prob': 0}
            lang_counts[lang]['count'] += 1
            lang_counts[lang]['max_prob'] = max(lang_counts[lang]['max_prob'], det['prob'])
        
        # Find most common language
        best_lang = 'en'
        best_score = 0
        
        for lang, stats in lang_counts.items():
            if lang in SUPPORTED_LANGUAGES:
                # Score based on frequency and max probability
                score = (stats['count'] / attempts) * stats['max_prob']
                if score > best_score:
                    best_score = score
                    best_lang = lang
        
        if LANGUAGE_DETECTION_CONFIG.get('debug_logging'):
            logger.info(f"Langdetect consensus: {best_lang} (score: {best_score:.2f})")
        
        return {'language': best_lang, 'confidence': best_score}
        
    except Exception as e:
        logger.error(f"Langdetect error: {str(e)}")
        return {'language': 'en', 'confidence': 0.1}

def gpt_detect_language(text: str) -> Dict[str, any]:
    """
    Use GPT for language detection
    """
    try:
        client = get_openai_client()
        if not client:
            return {'language': 'en', 'confidence': 0.0}
        
        # Only use GPT for longer texts
        if len(text.strip()) < LANGUAGE_DETECTION_CONFIG.get('gpt_min_length', 20):
            return {'language': 'en', 'confidence': 0.0}
        
        prompt = f"""Detect the language of this text. Reply with ONLY the ISO 639-1 code (e.g., en, es, fr).

Text: "{text[:500]}"

Language code:"""

        response = client.chat.completions.create(
            model=LANGUAGE_DETECTION_CONFIG.get('gpt_model', 'gpt-3.5-turbo'),
            messages=[{"role": "user", "content": prompt}],
            max_tokens=5,
            temperature=0
        )
        
        detected = response.choices[0].message.content.strip().lower()
        
        if LANGUAGE_DETECTION_CONFIG.get('debug_logging'):
            logger.info(f"GPT detected: {detected}")
        
        if detected in SUPPORTED_LANGUAGES:
            return {'language': detected, 'confidence': 0.9}
        
        return {'language': 'en', 'confidence': 0.5}
        
    except Exception as e:
        logger.error(f"GPT detection error: {str(e)}")
        return {'language': 'en', 'confidence': 0.0}

def hybrid_detect(text: str) -> str:
    """
    Simple hybrid detection: Use both methods and make an intelligent choice
    """
    # Quick script-based detection for obvious cases
    if any('\u4e00' <= char <= '\u9fff' for char in text):  # Chinese
        return 'zh'
    elif any('\u0600' <= char <= '\u06ff' for char in text):  # Arabic
        return 'ar'
    elif any('\u3040' <= char <= '\u309f' or '\u30a0' <= char <= '\u30ff' for char in text):  # Japanese
        return 'ja'
    elif any('\uac00' <= char <= '\ud7af' for char in text):  # Korean
        return 'ko'
    elif any('\u0400' <= char <= '\u04ff' for char in text):  # Cyrillic
        # Could be Russian, Bulgarian, Ukrainian, etc. - need more detection
        pass
    
    # Get langdetect result
    langdetect_result = langdetect_with_confidence(text)
    
    # For any text, also try GPT if enabled and available
    gpt_result = {'language': 'en', 'confidence': 0.0}
    if LANGUAGE_DETECTION_CONFIG.get('use_gpt', True):
        gpt_result = gpt_detect_language(text)
    
    # Known problematic patterns
    text_lower = text.lower()
    
    # Fix common misdetections
    if langdetect_result['language'] == 'it':
        # Check if it's actually Spanish
        spanish_indicators = ['hola', 'necesito', 'ayuda', 'gracias', 'por favor', 'qué', 'cómo', 'cuál']
        if any(word in text_lower for word in spanish_indicators):
            if LANGUAGE_DETECTION_CONFIG.get('debug_logging'):
                logger.info("Correcting Italian to Spanish based on keywords")
            return 'es'
        
        # Check if it's actually English
        english_indicators = ['hello', 'need', 'help', 'please', 'thank', 'what', 'how', 'citizen', 'migrate']
        if any(word in text_lower for word in english_indicators):
            if LANGUAGE_DETECTION_CONFIG.get('debug_logging'):
                logger.info("Correcting Italian to English based on keywords")
            return 'en'
    
    if langdetect_result['language'] == 'nl':
        # Check if it's actually English
        if 'hello' in text_lower or 'help' in text_lower or 'need' in text_lower:
            if LANGUAGE_DETECTION_CONFIG.get('debug_logging'):
                logger.info("Correcting Dutch to English based on keywords")
            return 'en'
    
    if langdetect_result['language'] == 'bg' and any('\u0400' <= char <= '\u04ff' for char in text):
        # Check if it's actually Russian
        russian_indicators = ['привет', 'помощь', 'нужна', 'мне']
        if any(word in text_lower for word in russian_indicators):
            if LANGUAGE_DETECTION_CONFIG.get('debug_logging'):
                logger.info("Correcting Bulgarian to Russian based on keywords")
            return 'ru'
    
    # If GPT has a valid result with good confidence, prefer it
    if gpt_result['confidence'] > 0.7:
        if LANGUAGE_DETECTION_CONFIG.get('debug_logging'):
            logger.info(f"Using GPT result: {gpt_result['language']} (confidence: {gpt_result['confidence']})")
        return gpt_result['language']
    
    # If langdetect has very low confidence, don't trust it
    if langdetect_result['confidence'] < 0.5:
        # Default to English for low confidence
        if LANGUAGE_DETECTION_CONFIG.get('debug_logging'):
            logger.info("Low langdetect confidence, defaulting to English")
        return 'en'
    
    # If langdetect is confident about a supported language, use it
    if langdetect_result['confidence'] > 0.7:
        if LANGUAGE_DETECTION_CONFIG.get('debug_logging'):
            logger.info(f"Using langdetect: {langdetect_result['language']} (confidence: {langdetect_result['confidence']})")
        return langdetect_result['language']
    
    # Default case
    return langdetect_result['language']

def detect_language(text: str) -> str:
    """
    Main language detection function
    
    Args:
        text: The text to detect language from
        
    Returns:
        ISO 639-1 language code (e.g., 'en', 'fr', 'es')
    """
    method = LANGUAGE_DETECTION_CONFIG.get('method', 'hybrid')
    
    if method == 'hybrid':
        return hybrid_detect(text)
    elif method == 'gpt':
        result = gpt_detect_language(text)
        return result['language']
    else:  # langdetect
        result = langdetect_with_confidence(text)
        return result['language']

def get_optimal_language_detection(text: str, speech_detected_language: str = None) -> str:
    """
    Get the most accurate language detection available.
    
    Priority:
    1. Use speech-detected language if available and supported
    2. Use configured detection method
    
    Args:
        text: The text content
        speech_detected_language: Language detected by speech API (if available)
        
    Returns:
        ISO 639-1 language code
    """
    # Priority 1: Use speech detection if available and supported
    if speech_detected_language and speech_detected_language in SUPPORTED_LANGUAGES:
        logger.info(f"Using speech-detected language: {speech_detected_language}")
        return speech_detected_language
    
    # Priority 2: Use configured detection method
    return detect_language(text)