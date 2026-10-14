"""
Language Detection Configuration
"""

# Simple configuration for the hybrid language detection system
LANGUAGE_DETECTION_CONFIG = {
    # Primary detection method: 'langdetect', 'gpt', 'hybrid'
    'method': 'hybrid',
    
    # Confidence threshold for accepting langdetect results (0.0 to 1.0)
    'confidence_threshold': 0.7,
    
    # Enable GPT detection (requires OpenAI API key)
    'use_gpt': True,
    
    # Minimum text length for GPT detection (to save API calls)
    'gpt_min_length': 10,
    
    # GPT model for language detection
    'gpt_model': 'gpt-3.5-turbo',
    
    # Number of langdetect attempts for consistency checking
    'langdetect_attempts': 3,
    
    # Enable debug logging
    'debug_logging': False,
}