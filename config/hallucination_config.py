"""
Configuration settings for hallucination detection and prevention
"""

HALLUCINATION_CONFIG = {
    # Detection thresholds for different methods
    'detection_thresholds': {
        'llm_detection': 0.3,  # LLM-based detection threshold
        'semantic_similarity': 0.5,  # Semantic similarity threshold
        'context_grounding': 0.7,  # Context grounding threshold
        'citation_verification': 0.8,  # Citation verification threshold
        'factual_verification': 0.6,  # Factual claims verification threshold
    },
    
    # Overall confidence thresholds
    'confidence_thresholds': {
        'warning_threshold': 0.4,  # Show warning to user
        'critical_threshold': 0.6,  # Block response or require review
        'high_confidence_threshold': 0.8,  # High confidence hallucination
    },
    
    # Optimized retrieval configuration
    'retrieval_config': {
        'base_k': 3,  # Reduced from 5 for faster retrieval
        'max_k': 6,   # Reduced from 10 for faster processing
        'similarity_threshold': 0.75,  # Increased from 0.7 for better quality
        'quality_weight': 0.3,  # Weight for document quality vs similarity
        'diversity_factor': 0.1,  # Factor to promote diversity in results
    },
    
    # Response generation configuration
    'response_config': {
        'temperature': 1.0,
        'require_citations': False,
        'max_tokens': 3000,
        'model': 'gpt-4.1',
        'confidence_threshold': 0.5,
    },
    
    # Method weights for aggregated scoring
    'method_weights': {
        'llm_detection': 0.35,
        'semantic_similarity': 0.25,
        'context_grounding': 0.20,
        'citation_verification': 0.10,
        'factual_verification': 0.10,
    },
    
    # Document quality scoring parameters
    'quality_scoring': {
        'min_length': 50,  # Minimum document length
        'max_length': 2000,  # Maximum useful document length
        'legal_term_boost': 0.2,  # Boost for legal terminology
        'citation_boost': 0.1,  # Boost for containing citations
        'metadata_boost': 0.1,  # Boost for good metadata
    },
    
    # Warning messages for different scenarios
    'warning_messages': {
        'low_similarity': "⚠️ This response may not be well-grounded in the provided documents.",
        'no_citations': "⚠️ This response lacks specific citations to source documents.",
        'high_hallucination': "⚠️ This response may contain information not supported by the provided documents. Please verify with original sources.",
        'poor_grounding': "⚠️ This response may include information beyond what's in the provided context.",
        'factual_concerns': "⚠️ Some factual claims in this response may need verification.",
    },
    
    # Analytics and monitoring settings
    'analytics': {
        'log_retention_days': 30,  # How long to keep detailed logs
        'summary_retention_days': 90,  # How long to keep daily summaries
        'trend_analysis_days': 14,  # Days to use for trend analysis
        'alert_thresholds': {
            'high_hallucination_rate': 0.15,  # Alert if >15% hallucination rate
            'sudden_increase': 2.0,  # Alert if 2x increase in hallucinations
        }
    },
    
    # Performance optimization settings
    'performance': {
        'enable_caching': True,
        'cache_ttl_seconds': 3600,  # 1 hour cache for retrieval results
        'batch_size': 50,  # For batch processing
        'max_concurrent_detections': 10,
        'timeout_seconds': 30,  # Detection timeout
        'preload_common_queries': True,  # Enable preloading of common queries
    },
    
    # Legal domain specific settings
    'legal_domain': {
        'legal_keywords': [
            'court', 'judge', 'lawyer', 'attorney', 'legal', 'law', 'act', 'section',
            'clause', 'regulation', 'statute', 'case', 'precedent', 'jurisdiction',
            'litigation', 'contract', 'agreement', 'liability', 'damages'
        ],
        'boost_legal_content': True,
        'require_legal_grounding': False,
    }
}

def get_config(environment: str = 'production') -> dict:
    """
    Get configuration based on environment
    """
    config = HALLUCINATION_CONFIG.copy()
    
    if environment == 'development':
        # More aggressive caching and logging in development
        config['performance']['cache_ttl_seconds'] = 1800  # 30 minutes
        config['retrieval_config']['base_k'] = 2  # Even faster for dev
        config['analytics']['log_retention_days'] = 7
        
    elif environment == 'testing':
        # Minimal caching for testing
        config['performance']['enable_caching'] = False
        config['retrieval_config']['base_k'] = 2
        
    return config

# Validation rules for configuration
CONFIG_VALIDATION = {
    'required_keys': [
        'detection_thresholds',
        'confidence_thresholds',
        'retrieval_config',
        'response_config'
    ],
    'threshold_ranges': {
        'detection_thresholds': (0.0, 1.0),
        'confidence_thresholds': (0.0, 1.0),
    },
    'positive_integers': [
        'retrieval_config.base_k',
        'retrieval_config.max_k',
        'analytics.log_retention_days'
    ]
}

def validate_config(config):
    """Validate configuration settings"""
    errors = []
    
    # Check required keys
    for key in CONFIG_VALIDATION['required_keys']:
        if key not in config:
            errors.append(f"Missing required config key: {key}")
    
    # Validate threshold ranges
    for threshold_group, (min_val, max_val) in CONFIG_VALIDATION['threshold_ranges'].items():
        if threshold_group in config:
            for threshold_name, value in config[threshold_group].items():
                if not (min_val <= value <= max_val):
                    errors.append(f"Threshold {threshold_group}.{threshold_name} = {value} not in range [{min_val}, {max_val}]")
    
    # Validate positive integers
    for path in CONFIG_VALIDATION['positive_integers']:
        keys = path.split('.')
        value = config
        for key in keys:
            if key in value:
                value = value[key]
            else:
                break
        else:
            if not isinstance(value, int) or value <= 0:
                errors.append(f"Config {path} must be a positive integer, got {value}")
    
    return errors 