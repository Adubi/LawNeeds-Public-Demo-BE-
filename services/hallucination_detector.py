import re
import logging
from typing import Dict, List, Tuple, Optional
from langchain_openai import ChatOpenAI
from sklearn.metrics.pairwise import cosine_similarity
import numpy as np
import os

logger = logging.getLogger(__name__)

class HallucinationDetector:
    def __init__(self, embedding_function, openai_api_key: str):
        self.embedding_function = embedding_function
        self.llm = ChatOpenAI(
            model_name="gpt-5-nano", 
            temperature=1.0,
            api_key=openai_api_key
        )

    def detect_hallucination(self, context: str, answer: str, question: str) -> Dict[str, any]:
        """
        Multi-method hallucination detection
        Returns a comprehensive analysis of potential hallucinations
        """
        results = {
            'is_hallucination': False,
            'confidence_score': 0.0,
            'methods': {},
            'warning_flags': []
        }
        
        # Method 1: LLM-based detection
        llm_result = self.llm_based_detection(context, answer)
        results['methods']['llm_detection'] = llm_result
        
        # Method 2: Semantic similarity
        similarity_result = self.semantic_similarity_detection(context, answer)
        results['methods']['semantic_similarity'] = similarity_result
        
        # Method 3: Context grounding check
        grounding_result = self.context_grounding_check(context, answer)
        results['methods']['context_grounding'] = grounding_result
        
        # Method 4: Citation verification
        citation_result = self.citation_verification(context, answer)
        results['methods']['citation_verification'] = citation_result
        
        # Method 5: Factual claims verification
        factual_result = self.factual_claims_verification(context, answer)
        results['methods']['factual_verification'] = factual_result
        
        # Aggregate results
        results = self.aggregate_detection_results(results)
        
        return results

    def llm_based_detection(self, context: str, answer: str) -> Dict[str, any]:
        """Use an LLM to detect hallucinations"""
        prompt = f"""
You are an expert fact-checker. Your task is to determine if the answer is based on the provided context.

Context: {context}

Answer: {answer}

Instructions:
1. Check if every claim in the answer can be found in the context
2. Identify any information that is not supported by the context
3. Rate the answer on a scale of 0-1 where:
   - 0 = Completely grounded in context
   - 1 = Contains significant hallucinations

Respond with ONLY a number between 0 and 1, followed by a brief explanation.
Format: [SCORE]|[EXPLANATION]
"""
        
        try:
            response = self.llm.invoke(prompt).content
            parts = response.split('|')
            score = float(parts[0].strip())
            explanation = parts[1].strip() if len(parts) > 1 else ""
            
            return {
                'score': score,
                'explanation': explanation,
                'is_hallucination': score > 0.3
            }
        except Exception as e:
            logger.error(f"LLM detection error: {e}")
            return {'score': 0.5, 'explanation': 'Error in detection', 'is_hallucination': False}

    def semantic_similarity_detection(self, context: str, answer: str) -> Dict[str, any]:
        """Detect hallucinations using semantic similarity"""
        try:
            # Get embeddings
            context_embedding = self.embedding_function.embed_query(context)
            answer_embedding = self.embedding_function.embed_query(answer)
            
            # Calculate similarity
            similarity = cosine_similarity(
                np.array(context_embedding).reshape(1, -1),
                np.array(answer_embedding).reshape(1, -1)
            )[0][0]
            
            # Convert to hallucination score (inverse of similarity)
            hallucination_score = 1 - similarity
            
            return {
                'similarity': similarity,
                'hallucination_score': hallucination_score,
                'is_hallucination': hallucination_score > 0.5
            }
        except Exception as e:
            logger.error(f"Semantic similarity error: {e}")
            return {'similarity': 0.5, 'hallucination_score': 0.5, 'is_hallucination': False}

    def context_grounding_check(self, context: str, answer: str) -> Dict[str, any]:
        """Check how well the answer is grounded in the context"""
        # Tokenize and clean text
        context_tokens = set(re.findall(r'\b\w+\b', context.lower()))
        answer_tokens = set(re.findall(r'\b\w+\b', answer.lower()))
        
        # Remove common stop words
        stop_words = {'the', 'a', 'an', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for', 'of', 'with', 'by', 'is', 'are', 'was', 'were', 'be', 'been', 'being', 'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would', 'should', 'could', 'can', 'may', 'might', 'must', 'shall'}
        
        context_tokens -= stop_words
        answer_tokens -= stop_words
        
        # Calculate token overlap
        if len(answer_tokens) == 0:
            overlap_ratio = 1.0
        else:
            overlap = len(answer_tokens.intersection(context_tokens))
            overlap_ratio = overlap / len(answer_tokens)
        
        # Calculate grounding score
        grounding_score = overlap_ratio
        hallucination_score = 1 - grounding_score
        
        return {
            'grounding_score': grounding_score,
            'hallucination_score': hallucination_score,
            'token_overlap': len(answer_tokens.intersection(context_tokens)),
            'total_answer_tokens': len(answer_tokens),
            'is_hallucination': hallucination_score > 0.7
        }

    def citation_verification(self, context: str, answer: str) -> Dict[str, any]:
        """Verify if claims in the answer can be traced to the context"""
        # Look for citation patterns in the answer
        citation_patterns = [
            r'according to.*?documents?',
            r'the.*?states?',
            r'as mentioned.*?above',
            r'the.*?shows?',
            r'based on.*?provided',
            r'according to.*?context',
            r'the document.*?indicates',
            r'as stated.*?in'
        ]
        
        has_citations = any(re.search(pattern, answer.lower()) for pattern in citation_patterns)
        
        # Check for specific claims that need verification
        claim_indicators = [
            r'\d+%',  # Percentages
            r'\$[\d,]+',  # Dollar amounts
            r'\d{4}',  # Years
            r'section \d+',  # Legal sections
            r'article \d+',  # Article numbers
            r'paragraph \d+',  # Paragraph numbers
            r'page \d+',  # Page numbers
            r'case.*?v\..*?',  # Case names
            r'\d+ days?',  # Time periods
            r'\d+ months?',
            r'\d+ years?'
        ]
        
        specific_claims = []
        for pattern in claim_indicators:
            matches = re.findall(pattern, answer.lower())
            specific_claims.extend(matches)
        
        # Verify specific claims in context
        verified_claims = []
        for claim in specific_claims:
            if claim in context.lower():
                verified_claims.append(claim)
        
        verification_ratio = len(verified_claims) / len(specific_claims) if specific_claims else 1.0
        
        return {
            'has_citations': has_citations,
            'specific_claims': specific_claims,
            'verified_claims': verified_claims,
            'verification_ratio': verification_ratio,
            'is_hallucination': verification_ratio < 0.8 and len(specific_claims) > 0
        }

    def factual_claims_verification(self, context: str, answer: str) -> Dict[str, any]:
        """Verify factual claims against the context"""
        # Split answer into sentences
        sentences = re.split(r'[.!?]+', answer)
        
        verified_sentences = 0
        total_sentences = len([s for s in sentences if s.strip()])
        
        # Check each sentence for grounding in context
        for sentence in sentences:
            sentence = sentence.strip()
            if not sentence:
                continue
                
            # Extract key phrases from sentence
            key_phrases = self.extract_key_phrases(sentence)
            
            # Check if key phrases exist in context
            phrase_matches = 0
            for phrase in key_phrases:
                if phrase.lower() in context.lower():
                    phrase_matches += 1
            
            # If most key phrases are found, consider sentence verified
            if key_phrases and phrase_matches / len(key_phrases) > 0.5:
                verified_sentences += 1
        
        verification_score = verified_sentences / total_sentences if total_sentences > 0 else 1.0
        
        return {
            'verified_sentences': verified_sentences,
            'total_sentences': total_sentences,
            'verification_score': verification_score,
            'is_hallucination': verification_score < 0.6
        }

    def extract_key_phrases(self, sentence: str) -> List[str]:
        """Extract key phrases from a sentence"""
        # Remove common words and extract meaningful phrases
        words = re.findall(r'\b\w+\b', sentence.lower())
        stop_words = {'the', 'a', 'an', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for', 'of', 'with', 'by', 'is', 'are', 'was', 'were', 'be', 'been', 'being', 'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would', 'should', 'could', 'can', 'may', 'might', 'must', 'shall', 'this', 'that', 'these', 'those'}
        
        key_words = [word for word in words if word not in stop_words and len(word) > 2]
        
        # Also extract number and date patterns
        numbers = re.findall(r'\d+', sentence)
        dates = re.findall(r'\d{4}', sentence)
        
        return key_words + numbers + dates

    def aggregate_detection_results(self, results: Dict) -> Dict:
        """Aggregate results from all detection methods"""
        methods = results['methods']
        
        # Calculate weighted confidence score
        scores = []
        weights = []
        
        # LLM detection (highest weight)
        if 'llm_detection' in methods:
            scores.append(methods['llm_detection']['score'])
            weights.append(0.35)
        
        # Semantic similarity
        if 'semantic_similarity' in methods:
            scores.append(methods['semantic_similarity']['hallucination_score'])
            weights.append(0.25)
        
        # Context grounding
        if 'context_grounding' in methods:
            scores.append(methods['context_grounding']['hallucination_score'])
            weights.append(0.20)
        
        # Citation verification
        if 'citation_verification' in methods:
            citation_score = 1 - methods['citation_verification']['verification_ratio']
            scores.append(citation_score)
            weights.append(0.10)
        
        # Factual verification
        if 'factual_verification' in methods:
            factual_score = 1 - methods['factual_verification']['verification_score']
            scores.append(factual_score)
            weights.append(0.10)
        
        # Weighted average
        if scores and weights:
            confidence_score = sum(s * w for s, w in zip(scores, weights)) / sum(weights)
        else:
            confidence_score = 0.5
        
        # Determine if it's a hallucination
        is_hallucination = confidence_score > 0.4
        
        # Add warning flags
        warning_flags = []
        if methods.get('semantic_similarity', {}).get('similarity', 1) < 0.3:
            warning_flags.append("Low semantic similarity with context")
        
        if methods.get('context_grounding', {}).get('grounding_score', 1) < 0.3:
            warning_flags.append("Poor grounding in provided context")
        
        if not methods.get('citation_verification', {}).get('has_citations', True):
            warning_flags.append("No citations or references to source material")
        
        if methods.get('factual_verification', {}).get('verification_score', 1) < 0.5:
            warning_flags.append("Factual claims not well supported by context")
        
        if methods.get('llm_detection', {}).get('score', 0) > 0.6:
            warning_flags.append("AI model detected potential hallucination")
        
        results.update({
            'is_hallucination': is_hallucination,
            'confidence_score': confidence_score,
            'warning_flags': warning_flags
        })
        
        return results

    def quick_detection(self, context: str, answer: str) -> Dict[str, any]:
        """Quick hallucination detection using only fast methods"""
        results = {
            'is_hallucination': False,
            'confidence_score': 0.0,
            'method': 'quick_detection'
        }
        
        # Quick semantic similarity check
        try:
            context_embedding = self.embedding_function.embed_query(context)
            answer_embedding = self.embedding_function.embed_query(answer)
            
            similarity = cosine_similarity(
                np.array(context_embedding).reshape(1, -1),
                np.array(answer_embedding).reshape(1, -1)
            )[0][0]
            
            # Quick grounding check
            context_tokens = set(re.findall(r'\b\w{3,}\b', context.lower()))
            answer_tokens = set(re.findall(r'\b\w{3,}\b', answer.lower()))
            
            if len(answer_tokens) > 0:
                overlap_ratio = len(answer_tokens.intersection(context_tokens)) / len(answer_tokens)
            else:
                overlap_ratio = 1.0
            
            # Combine scores
            combined_score = (similarity * 0.6) + (overlap_ratio * 0.4)
            confidence_score = 1 - combined_score
            
            results.update({
                'is_hallucination': confidence_score > 0.5,
                'confidence_score': confidence_score,
                'similarity': similarity,
                'overlap_ratio': overlap_ratio
            })
            
        except Exception as e:
            logger.error(f"Quick detection error: {e}")
            results['confidence_score'] = 0.5
        
        return results 