import hashlib
import json
import logging
import time
import asyncio
from typing import List, Dict, Optional, Tuple, Any
from langchain_core.documents import Document
from langchain_pinecone import PineconeVectorStore
from datetime import datetime

logger = logging.getLogger(__name__)

class OptimizedRetrievalService:
    """
    Optimized vector search service with caching, smart filtering, and performance monitoring
    """
    
    def __init__(self, index, embedding_function, redis_service, hallucination_config=None):
        self.index = index
        self.embedding_function = embedding_function
        self.redis_service = redis_service
        self.hallucination_config = hallucination_config or {}
        
        # Performance monitoring
        self.metrics = {
            'cache_hits': 0,
            'cache_misses': 0,
            'total_queries': 0,
            'avg_retrieval_time': 0.0
        }
        
        # Cache configuration
        self.cache_config = self.hallucination_config.get('performance', {})
        self.enable_caching = self.cache_config.get('enable_caching', True)
        self.cache_ttl = self.cache_config.get('cache_ttl_seconds', 3600)
        
        # Retrieval configuration with optimized defaults
        self.retrieval_config = self.hallucination_config.get('retrieval_config', {})
        self.base_k = self.retrieval_config.get('base_k', 3)  # Reduced from 5
        self.max_k = self.retrieval_config.get('max_k', 6)    # Reduced from 10
        self.similarity_threshold = self.retrieval_config.get('similarity_threshold', 0.75)  # Increased
        
        # Initialize vectorstore once for reuse
        self.vectorstore = PineconeVectorStore(
            self.index,
            self.embedding_function,
            text_key="text",
            namespace="default"
        )
        
        logger.info(f"OptimizedRetrievalService initialized with k={self.base_k}, max_k={self.max_k}")

    async def retrieve_documents(self, question: str, k: Optional[int] = None, 
                                language: str = "en") -> List[Document]:
        """
        Main retrieval method with caching and optimization
        """
        start_time = time.time()
        self.metrics['total_queries'] += 1
        
        # Use provided k or default
        k = k or self.base_k
        
        try:
            # Check cache first
            cached_result = await self._get_cached_result(question, k, language)
            if cached_result is not None:
                self.metrics['cache_hits'] += 1
                logger.info(
                    "Retrieval cache hit",
                    extra={
                        "language": language,
                        "k": k,
                        "question_length": len(question or ""),
                    },
                )
                return cached_result
            
            self.metrics['cache_misses'] += 1
            
            # Perform optimized retrieval
            docs = await self._perform_optimized_retrieval(question, k)
            
            # Cache the result
            await self._cache_result(question, k, language, docs)
            
            # Update metrics
            retrieval_time = time.time() - start_time
            self._update_metrics(retrieval_time)
            
            logger.info(
                "Retrieved documents",
                extra={
                    "language": language,
                    "k": k,
                    "doc_count": len(docs),
                    "retrieval_time_seconds": round(retrieval_time, 3),
                    "question_length": len(question or ""),
                },
            )
            return docs
            
        except Exception as e:
            logger.error(f"Error in optimized retrieval: {e}")
            # Fallback to basic retrieval
            return await self._fallback_retrieval(question, k)

    async def _perform_optimized_retrieval(self, question: str, k: int) -> List[Document]:
        """
        Optimized retrieval with smart filtering and reduced API calls
        """
        try:
            # Strategy 1: Start with fewer documents to reduce processing time
            initial_k = min(k * 1.5, self.max_k)  # Reduced multiplier from 2x to 1.5x
            
            # Single retrieval call instead of multiple
            retriever = self.vectorstore.as_retriever(
                search_type="similarity_score_threshold",  # Use threshold-based search
                search_kwargs={
                    "k": int(initial_k),
                    "score_threshold": self.similarity_threshold,
                    "namespace": "default"
                }
            )
            
            # Get documents with scores
            docs_with_scores = retriever.invoke(question)
            
            # If we don't get enough high-quality docs, fallback to regular similarity
            if len(docs_with_scores) < k:
                logger.info(f"Threshold search returned {len(docs_with_scores)} docs, falling back to similarity")
                retriever = self.vectorstore.as_retriever(
                    search_type="similarity",
                    search_kwargs={
                        "k": int(initial_k),
                        "namespace": "default"
                    }
                )
                docs_with_scores = retriever.invoke(question)
            
            # Fast quality filtering without expensive re-ranking
            filtered_docs = self._fast_quality_filter(question, docs_with_scores, k)
            
            return filtered_docs
            
        except Exception as e:
            logger.error(f"Error in optimized retrieval: {e}")
            raise

    def _fast_quality_filter(self, question: str, docs: List[Document], target_k: int) -> List[Document]:
        """
        Fast quality filtering without expensive embedding calculations
        """
        if len(docs) <= target_k:
            return docs
        
        try:
            scored_docs = []
            question_lower = question.lower()
            
            for doc in docs:
                content = doc.page_content.lower()
                
                # Fast quality scoring based on content analysis
                quality_score = 0.5  # Base score
                
                # Keyword overlap scoring (fast)
                question_words = set(question_lower.split())
                content_words = set(content.split())
                overlap_ratio = len(question_words.intersection(content_words)) / len(question_words)
                quality_score += overlap_ratio * 0.3
                
                # Length scoring (avoid too short/long docs)
                content_length = len(content)
                if 100 <= content_length <= 1500:  # Optimal length range
                    quality_score += 0.1
                elif content_length < 50:  # Too short
                    quality_score -= 0.2
                elif content_length > 3000:  # Too long
                    quality_score -= 0.1
                
                # Legal term boost (fast regex-free approach)
                legal_indicators = ['law', 'legal', 'court', 'act', 'section', 'clause', 'regulation']
                legal_count = sum(1 for term in legal_indicators if term in content)
                quality_score += min(legal_count * 0.05, 0.2)
                
                # Metadata scoring
                metadata = getattr(doc, 'metadata', {})
                if metadata.get('source') or metadata.get('title'):
                    quality_score += 0.1
                
                scored_docs.append((doc, quality_score))
            
            # Sort by quality score and return top k
            scored_docs.sort(key=lambda x: x[1], reverse=True)
            return [doc for doc, _ in scored_docs[:target_k]]
            
        except Exception as e:
            logger.error(f"Error in fast quality filtering: {e}")
            return docs[:target_k]

    async def _get_cached_result(self, question: str, k: int, language: str) -> Optional[List[Document]]:
        """
        Get cached retrieval result
        """
        if not self.enable_caching:
            return None
            
        try:
            cache_key = self._generate_cache_key(question, k, language)
            cached_data = await self.redis_service.get_key(cache_key)
            
            if cached_data:
                data = json.loads(cached_data)
                # Reconstruct Document objects
                docs = []
                for doc_data in data['documents']:
                    doc = Document(
                        page_content=doc_data['page_content'],
                        metadata=doc_data['metadata']
                    )
                    docs.append(doc)
                return docs
                
        except Exception as e:
            logger.warning(f"Error retrieving from cache: {e}")
            
        return None

    async def _cache_result(self, question: str, k: int, language: str, docs: List[Document]):
        """
        Cache retrieval result
        """
        if not self.enable_caching:
            return
            
        try:
            cache_key = self._generate_cache_key(question, k, language)
            
            # Serialize documents
            docs_data = []
            for doc in docs:
                docs_data.append({
                    'page_content': doc.page_content,
                    'metadata': doc.metadata
                })
            
            cache_data = {
                'documents': docs_data,
                'timestamp': datetime.now().isoformat(),
                'question': question[:100],  # Store truncated question for debugging
                'k': k
            }
            
            await self.redis_service.set_key(
                cache_key, 
                json.dumps(cache_data), 
                expire=self.cache_ttl
            )
            
        except Exception as e:
            logger.warning(f"Error caching result: {e}")

    def _generate_cache_key(self, question: str, k: int, language: str) -> str:
        """
        Generate cache key for the query
        """
        # Normalize question for better cache hits
        normalized_question = question.lower().strip()
        
        # Create hash of question + parameters
        cache_input = f"{normalized_question}:{k}:{language}"
        cache_hash = hashlib.md5(cache_input.encode()).hexdigest()
        
        return f"retrieval_cache:{cache_hash}"

    async def _fallback_retrieval(self, question: str, k: int) -> List[Document]:
        """
        Simple fallback retrieval method
        """
        try:
            retriever = self.vectorstore.as_retriever(
                search_kwargs={"k": k, "namespace": "default"}
            )
            return retriever.invoke(question)
        except Exception as e:
            logger.error(f"Fallback retrieval failed: {e}")
            return []

    def _update_metrics(self, retrieval_time: float):
        """
        Update performance metrics
        """
        # Update average retrieval time
        current_avg = self.metrics['avg_retrieval_time']
        total_queries = self.metrics['total_queries']
        
        self.metrics['avg_retrieval_time'] = (
            (current_avg * (total_queries - 1)) + retrieval_time
        ) / total_queries

    async def get_performance_metrics(self) -> Dict[str, Any]:
        """
        Get performance metrics for monitoring
        """
        cache_hit_rate = 0.0
        if self.metrics['total_queries'] > 0:
            cache_hit_rate = self.metrics['cache_hits'] / self.metrics['total_queries']
        
        return {
            'total_queries': self.metrics['total_queries'],
            'cache_hit_rate': cache_hit_rate,
            'cache_hits': self.metrics['cache_hits'],
            'cache_misses': self.metrics['cache_misses'],
            'avg_retrieval_time': self.metrics['avg_retrieval_time'],
            'cache_enabled': self.enable_caching,
            'current_config': {
                'base_k': self.base_k,
                'max_k': self.max_k,
                'similarity_threshold': self.similarity_threshold,
                'cache_ttl': self.cache_ttl
            }
        }

    async def clear_cache(self, pattern: str = "retrieval_cache:*") -> int:
        """
        Clear retrieval cache
        """
        try:
            deleted = 0
            async for key in self.redis_service.redis.scan_iter(match=pattern):
                await self.redis_service.redis.delete(key)
                deleted += 1
            
            logger.info(f"Cleared {deleted} cached retrieval results")
            return deleted
            
        except Exception as e:
            logger.error(f"Error clearing cache: {e}")
            return 0

    async def preload_common_queries(self, common_queries: List[str]):
        """
        Preload cache with common queries for faster response
        """
        logger.info(f"Preloading {len(common_queries)} common queries...")
        
        for query in common_queries:
            try:
                await self.retrieve_documents(query)
                await asyncio.sleep(0.1)  # Small delay to avoid overwhelming the system
            except Exception as e:
                logger.warning(f"Failed to preload query '{query[:50]}...': {e}")
        
        logger.info("Preloading completed")
