import json
import lzstring
from fastapi import HTTPException, APIRouter, Query, Request, Body, status, Form, UploadFile, File
from fastapi.responses import StreamingResponse, JSONResponse
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.output_parsers import StrOutputParser
from langchain_core.runnables import RunnablePassthrough
from langchain_pinecone import PineconeVectorStore
from pinecone import Pinecone
import os
from dotenv import load_dotenv
import logging
import traceback
from langchain.schema import StrOutputParser
from langchain.schema.runnable import RunnablePassthrough, RunnableParallel
from services.prompts import LegalPromptTemplates
from langchain.prompts import ChatPromptTemplate
import re
from typing import List, Dict, Any, Optional, Tuple
from services.redis_service import RedisService
import uuid
from openai import OpenAI
import base64
from io import BytesIO
from pydantic import BaseModel
from services.vector_db import VectorDB
from services.language_detection import detect_language, get_optimal_language_detection
from datetime import datetime, timezone
from services.models import ChatResponseFeedback, Conversation, ChatMessage, User
from sqlalchemy.orm import Session
from services.optimized_retrieval import OptimizedRetrievalService
from services.legal_rag_retrieval import LegalRAGRetrievalService, LEGAL_RAG_RETRIEVAL_ENABLED
from services.conversation_service import ConversationService
from services.s3_service import S3Service
from sqlalchemy import and_, func, String
import asyncio

# Import subscription service for query validation
try:
    from services.subscription_service import SubscriptionService
    from services.subscription_constants import UsageActionType
    SUBSCRIPTION_FEATURES_ENABLED = True
except ImportError as e:
    logging.warning(f"Subscription features not available: {e}")
    SUBSCRIPTION_FEATURES_ENABLED = False

# Load environment variables from the .env file
load_dotenv()

logger = logging.getLogger(__name__)

REFERRAL_CONTEXT_TTL_SECONDS = 30 * 24 * 60 * 60

# Import anti-hallucination components
try:
    from services.hallucination_detector import HallucinationDetector
    from services.hallucination_analytics import HallucinationAnalytics
    from config.hallucination_config import get_config
    HALLUCINATION_FEATURES_ENABLED = True
except ImportError as e:
    logger.warning(f"Hallucination detection features not available: {e}")
    HALLUCINATION_FEATURES_ENABLED = False

# Additional imports for enhanced retrieval
try:
    from sklearn.metrics.pairwise import cosine_similarity
    import numpy as np
    ENHANCED_RETRIEVAL_ENABLED = True
except ImportError:
    logger.warning("Enhanced retrieval features not available (sklearn missing)")
    ENHANCED_RETRIEVAL_ENABLED = False

# Load environment variables from the .env file
load_dotenv()

logger = logging.getLogger(__name__)

# Define response model
class TranscriptionResponse(BaseModel):
    text: str
    success: str

# Define request model
class AudioData(BaseModel):
    audio_data: str

def make_json_serializable(obj):
    """Convert numpy/sklearn types to JSON serializable types"""
    if hasattr(obj, 'item'):  # numpy scalar
        return obj.item()
    elif hasattr(obj, 'tolist'):  # numpy array
        return obj.tolist()
    elif isinstance(obj, dict):
        return {k: make_json_serializable(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [make_json_serializable(item) for item in obj]
    elif hasattr(obj, '__dict__'):  # custom objects
        return str(obj)
    else:
        return obj

def json_serializable(obj):
    """Make objects JSON serializable - comprehensive numpy and Python type handling"""
    import numpy as np
    import json
    
    # Handle None first
    if obj is None:
        return None
    
    # Try direct serialization first (for standard Python types)
    try:
        json.dumps(obj)
        return obj
    except (TypeError, ValueError):
        pass
    
    # Handle numpy types comprehensively
    if hasattr(obj, 'dtype'):  # Any numpy type
        if isinstance(obj, np.bool_):
            return bool(obj)
        elif isinstance(obj, np.integer):
            return int(obj)
        elif isinstance(obj, np.floating):
            return float(obj)
        elif isinstance(obj, np.ndarray):
            return obj.tolist()
        elif hasattr(obj, 'item'):  # All other numpy scalars
            return obj.item()
        elif hasattr(obj, 'tolist'):
            return obj.tolist()
    
    # Handle collections recursively
    if isinstance(obj, dict):
        return {str(k): json_serializable(v) for k, v in obj.items()}
    elif isinstance(obj, (list, tuple)):
        return [json_serializable(item) for item in obj]
    elif isinstance(obj, set):
        return list(obj)
    
    # Handle datetime objects
    if hasattr(obj, 'isoformat'):
        return obj.isoformat()
    
    # Last resort - convert to string
    return str(obj)

class ChatAPI:
    def __init__(self, *, app_name, db, auth):
        self.app_name = app_name
        self.db = db
        self.auth = auth
        self.redis_service = RedisService()
        
        # Initialize conversation service
        self.conversation_service = ConversationService(db, self.redis_service)
        
        # Initialize S3 service for image uploads
        self.s3_service = S3Service()
        
        # Load hallucination detection configuration if available
        if HALLUCINATION_FEATURES_ENABLED:
            environment = os.getenv('ENVIRONMENT', 'production')
            self.hallucination_config = get_config(environment)
            logger.info("Hallucination detection system enabled")
        else:
            self.hallucination_config = None
            logger.warning("Hallucination detection system disabled")
        
        # Use the existing VectorDB class instead of direct Pinecone initialization
        try:
            # Initialize VectorDB which handles Pinecone connection
            self.vector_db = VectorDB()
            
            # Get the index from the VectorDB instance
            self.index = self.vector_db.index
            
            logger.info("Successfully obtained Pinecone index from VectorDB")
            
            # Test the connection immediately to verify it works
            try:
                # Simple stats check to verify connection
                stats = self.index.describe_index_stats()
                logger.info(f"Pinecone index stats: {stats}")
            except Exception as e:
                logger.error(f"Error connecting to Pinecone index: {e}")
        except Exception as e:
            logger.error(f"Failed to initialize VectorDB: {e}")
            self.vector_db = None
            self.index = None
        
        # Add embedding function initialization
        self.embedding_function = OpenAIEmbeddings(model="text-embedding-3-small",api_key=os.getenv("SECRET_KEY"))
        self.prompt_templates = LegalPromptTemplates()
        self.openai_client = OpenAI(api_key=os.getenv('SECRET_KEY'))
        
        # Initialize optimized retrieval service
        if self.index:
            self.optimized_retrieval = OptimizedRetrievalService(
                index=self.index,
                embedding_function=self.embedding_function,
                redis_service=self.redis_service,
                hallucination_config=self.hallucination_config
            )
            logger.info("Optimized retrieval service initialized")
        else:
            self.optimized_retrieval = None
            logger.warning("Optimized retrieval service not available")

        if self.index and LEGAL_RAG_RETRIEVAL_ENABLED:
            self.legal_rag_retrieval = LegalRAGRetrievalService(
                index=self.index,
                embedding_function=self.embedding_function,
                db=self.db,
            )
            logger.info("Legal RAG retrieval service initialized")
        else:
            self.legal_rag_retrieval = None
            logger.warning("Legal RAG retrieval service not available")
        
        # Initialize hallucination detector and analytics if available
        if HALLUCINATION_FEATURES_ENABLED:
            try:
                self.hallucination_detector = HallucinationDetector(
                    embedding_function=self.embedding_function,
                    openai_api_key=os.getenv("SECRET_KEY")
                )
                self.hallucination_analytics = HallucinationAnalytics(self.redis_service)
                logger.info("Hallucination detection components initialized")
            except Exception as e:
                logger.error(f"Failed to initialize hallucination detection: {e}")
                self.hallucination_detector = None
                self.hallucination_analytics = None
        else:
            self.hallucination_detector = None
            self.hallucination_analytics = None

    async def _async_hallucination_validation(self, conversation_id: str, user_id: str, context: str, response: str, question: str):
        """Run hallucination validation asynchronously without blocking response"""
        try:
            validation_result = self.hallucination_detector.detect_hallucination(
                context=context,
                answer=response,
                question=question
            )
            
            # Make validation result JSON serializable BEFORE passing to analytics
            validation_result = json_serializable(validation_result)
            
            # Store validation analytics
            if self.hallucination_analytics:
                await self.hallucination_analytics.log_validation_result(
                    conversation_id=conversation_id,
                    user_id=user_id,
                    validation_result=validation_result,
                    question=question,
                    context_length=len(context)
                )
            
            logger.info("Background hallucination validation completed")
        except Exception as e:
            logger.warning(f"Background hallucination validation failed: {e}")

    # Add this method to the ChatAPI class
    async def _log_hallucination_analytics_background(self, conversation_id: str, user_id: str, validation_result: Dict, question: str, context_length: int):
        """Log hallucination analytics in background without blocking response"""
        try:
            await self.hallucination_analytics.log_validation_result(
                conversation_id=conversation_id,
                user_id=user_id,
                validation_result=validation_result,
                question=question,
                context_length=context_length
            )
            logger.debug("Hallucination analytics logged successfully")
        except Exception as e:
            logger.warning(f"Background hallucination logging failed: {e}")

    async def enhanced_retrieval_with_filtering(self, question: str, k: int = None, language: str = "en") -> List:
        """Enhanced retrieval with optimization and caching"""
        if self.optimized_retrieval:
            try:
                return await self.optimized_retrieval.retrieve_documents(
                    question=question, 
                    k=k, 
                    language=language
                )
            except Exception as e:
                logger.error(f"Optimized retrieval failed, falling back: {e}")
                return self._fallback_retrieval(question, k or 3)
        else:
            return self._fallback_retrieval(question, k or 3)

    async def legal_rag_retrieval_with_sources(
        self,
        question: str,
        k: int = 5,
        language: str = "en",
    ) -> Tuple[List, str, List[Dict[str, Any]], Dict[str, Any]]:
        """Retrieve legal-aware context with source references and fallback to legacy retrieval."""
        if self.legal_rag_retrieval:
            try:
                result = self.legal_rag_retrieval.retrieve(question, k=k)
                if result.documents:
                    return result.documents, result.context, result.sources, result.intent
            except Exception as e:
                logger.error(f"Legal RAG retrieval failed, falling back: {e}")

        fallback_docs = await self.enhanced_retrieval_with_filtering(question=question, k=k, language=language)
        fallback_context = "\n\n".join([
            LegalRAGRetrievalService.strip_internal_storage_urls(doc.page_content)
            for doc in fallback_docs
        ])
        return fallback_docs, fallback_context, [], {}

    def _fallback_retrieval(self, question: str, k: int) -> List:
        """Fallback to original retrieval method"""
        try:
            vectorstore = PineconeVectorStore(
                self.index,
                self.embedding_function,
                text_key="text",
                namespace="default"
            )
            retriever = vectorstore.as_retriever(search_kwargs={"k": k, "score_threshold": 0.75})
            return retriever.invoke(question)
        except Exception as e:
            logger.error(f"Fallback retrieval also failed: {e}")
            return []

    def filter_and_rerank_documents(self, question: str, docs: List, target_k: int) -> List:
        """Filter and re-rank documents based on relevance and quality"""
        if not ENHANCED_RETRIEVAL_ENABLED:
            return docs[:target_k]
            
        try:
            # Calculate relevance scores using embeddings
            question_embedding = self.embedding_function.embed_query(question)
            
            scored_docs = []
            for doc in docs:
                # Calculate semantic similarity
                doc_embedding = self.embedding_function.embed_query(doc.page_content)
                similarity = self.calculate_cosine_similarity(question_embedding, doc_embedding)
                
                # Quality filters
                quality_score = self.calculate_document_quality(doc)
                
                # Combined score
                quality_weight = self.hallucination_config.get('retrieval_config', {}).get('quality_weight', 0.3)
                final_score = (similarity * (1 - quality_weight)) + (quality_score * quality_weight)
                
                scored_docs.append((doc, final_score, similarity, quality_score))
            
            # Sort by score and return top k
            scored_docs.sort(key=lambda x: x[1], reverse=True)
            
            # Keep logs metadata-only; user questions may contain sensitive legal details.
            logger.info("Document scoring completed")
            for i, (doc, final_score, similarity, quality) in enumerate(scored_docs[:target_k]):
                logger.info(f"Doc {i+1}: Final={final_score:.3f}, Sim={similarity:.3f}, Qual={quality:.3f}")
            
            return [doc for doc, _, _, _ in scored_docs[:target_k]]
            
        except Exception as e:
            logger.error(f"Error in filtering documents: {e}")
            return docs[:target_k]

    def calculate_document_quality(self, doc) -> float:
        """Calculate document quality score"""
        content = doc.page_content
        metadata = getattr(doc, 'metadata', {})
        
        quality_score = 0.5  # Base score
        
        if not self.hallucination_config:
            return quality_score
        
        config = self.hallucination_config.get('quality_scoring', {})
        
        # Length check
        min_length = config.get('min_length', 50)
        max_length = config.get('max_length', 2000)
        if min_length <= len(content) <= max_length:
            quality_score += 0.2
        
        # Check for legal terminology
        legal_keywords = self.hallucination_config.get('legal_domain', {}).get('legal_keywords', [])
        legal_term_count = sum(1 for term in legal_keywords if term.lower() in content.lower())
        if legal_term_count > 0:
            legal_boost = config.get('legal_term_boost', 0.2)
            quality_score += min(legal_boost * legal_term_count / 5, legal_boost)
        
        # Check for citations
        citation_patterns = self.hallucination_config.get('legal_domain', {}).get('case_citation_patterns', [])
        for pattern in citation_patterns:
            if re.search(pattern, content):
                quality_score += config.get('citation_boost', 0.1)
                break
        
        # Check metadata quality
        if metadata and 'source' in metadata:
            quality_score += config.get('metadata_boost', 0.1)
        
        return min(quality_score, 1.0)

    def calculate_cosine_similarity(self, vec1, vec2):
        """Calculate cosine similarity between two vectors"""
        if not ENHANCED_RETRIEVAL_ENABLED:
            return 0.5  # Default similarity
            
        vec1 = np.array(vec1).reshape(1, -1)
        vec2 = np.array(vec2).reshape(1, -1)
        return cosine_similarity(vec1, vec2)[0][0]

    async def get_appropriate_template(
        self,
        question: str,
        context: str,
        language: str,
        source_references: Optional[List[Dict[str, Any]]] = None,
        retrieval_intent: Optional[Dict[str, Any]] = None,
    ) -> ChatPromptTemplate:
        """
        Determine the most appropriate template based on the question and context
        
        Args:
            question: The user's question
            context: The retrieved context
            language: The detected language code
        """
        try:
            logger.info(f"Selecting template for language: {language}")
            source_references = source_references or []
            retrieval_intent = retrieval_intent or {}
            
            # First check for referral intent - this takes priority over other templates
            referral_intent = self.prompt_templates.detect_referral_intent(question)
            if referral_intent['detected'] and referral_intent['confidence'] > 0.6:
                logger.info(f"Detected referral intent (confidence: {referral_intent['confidence']:.2f}) - using referral template in {language}")
                logger.info(f"Legal area: {referral_intent['legal_area']}, Urgency: {referral_intent['urgency']}")
                logger.info(f"Matched referral phrase count: {len(referral_intent.get('specific_phrases') or [])}")
                return self.prompt_templates.get_referral_template(
                    language=language,
                    legal_area=referral_intent['legal_area'],
                    urgency=referral_intent['urgency']
                )
            
            # Get response configuration
            if self.hallucination_config:
                response_config = self.hallucination_config.get('response_config', {})
                
                # Use citation enforcing template if citations are required
                if response_config.get('require_citations', False):
                    logger.info(f"Using citation enforcing template in {language}")
                    return self.prompt_templates.get_citation_enforcing_template(language)
            
            # Simple keyword-based template selection, with retrieval metadata as
            # an additional signal for the legal-aware RAG path.
            question_lower = question.lower()
            context_lower = (context or "").lower()
            document_type = (retrieval_intent.get("document_type") or "").lower()
            has_source_references = bool(source_references)
            
            if (
                any(word in question_lower for word in ['case', 'ruling', 'decision', 'court'])
                or document_type == "case"
                or "document type: case" in context_lower
            ):
                logger.info(f"Using case law template in {language}")
                return self.prompt_templates.get_case_law_template(language)
            
            elif any(word in question_lower for word in ['contract', 'agreement', 'clause']):
                logger.info(f"Using contract review template in {language}")
                return self.prompt_templates.get_contract_review_template(language)
            
            elif any(word in question_lower for word in ['analyze', 'review', 'document']):
                logger.info(f"Using document analysis template in {language}")
                return self.prompt_templates.get_document_analysis_template(language)

            elif has_source_references and (
                retrieval_intent.get("wants_latest")
                or document_type in {"guidance", "legislation"}
                or "document type: guidance" in context_lower
                or "document type: legislation" in context_lower
            ):
                logger.info(f"Using legal RAG citation template in {language}")
                return self.prompt_templates.get_legal_rag_citation_template(language)
            
            # Default to enhanced Q&A template
            logger.info(f"Using enhanced Q&A template in {language}")
            return self.prompt_templates.get_qa_template(language)
        except Exception as e:
            logger.error(f"Error selecting template: {e}")
            # Fallback to basic template
            return self.prompt_templates.get_qa_template("en")

    def sanitize_text(self, text: str) -> str:
        """Sanitize text by removing invalid Unicode characters"""
        if not isinstance(text, str):
            return str(text)
        
        # Remove surrogate characters
        text = text.encode('utf-8', errors='ignore').decode('utf-8')
        
        # Replace emojis and special characters with their text representation
        text = text.encode('ascii', errors='ignore').decode('ascii')
        
        # Remove any remaining control characters
        text = "".join(char for char in text if ord(char) >= 32 or char in "\n\r\t")
        
        return text.strip()

    def sanitize_messages(self, messages: List) -> List:
        """Sanitize a list of messages (can be LangChain message objects or dicts)"""
        sanitized_messages = []
        for message in messages:
            if isinstance(message, (AIMessage, HumanMessage)):
                # Handle LangChain message objects
                content = self.sanitize_text(message.content)
                # Create new message with sanitized content
                new_message = type(message)(content=content)
                sanitized_messages.append(new_message)
            elif isinstance(message, dict):
                # Handle dictionary messages
                content = self.sanitize_text(message.get('content', ''))
                sanitized_message = message.copy()
                sanitized_message['content'] = content
                sanitized_messages.append(sanitized_message)
            else:
                # Unknown type, add as-is
                sanitized_messages.append(message)
        return sanitized_messages

    async def _get_next_message_index(self, conversation_id: str, user_id: str = "") -> int:
        """Get the next message index for a conversation"""
        try:
            # Try Redis first (faster)
            history = await self.redis_service.get_chat_history(conversation_id)
            
            if history:
                return len(history)
            
            # Fallback to database through conversation service if user_id available
            if user_id:
                db_history = await self.conversation_service._get_history_from_db(
                    conversation_id, user_id, 1000  # Get all messages to count
                )
                return len(db_history)
            
            # Last resort: start from 0
            return 0
            
        except Exception as e:
            logger.error(f"Error getting next message index: {e}")
            return 0

    async def _reserve_message_indexes(self, conversation_id: str, user_id: str = "") -> tuple[int, int]:
        """Atomically reserve adjacent user/assistant message indexes for a turn."""
        counter_key = f"chat_message_index:{conversation_id}"
        try:
            redis_client = getattr(self.redis_service, "redis", None)
            if redis_client is not None:
                current_index = await self._get_next_message_index(conversation_id, user_id)
                await redis_client.set(counter_key, current_index, nx=True, ex=60 * 60 * 24)
                next_value = await redis_client.incrby(counter_key, 2)
                await redis_client.expire(counter_key, 60 * 60 * 24)
                user_message_index = max(0, int(next_value) - 2)
                return user_message_index, user_message_index + 1
        except Exception as e:
            logger.warning(f"Could not reserve message indexes in Redis: {e}")

        user_message_index = await self._get_next_message_index(conversation_id, user_id)
        return user_message_index, user_message_index + 1

    def _log_deprecation_warning(self, endpoint: str, user_id: str):
        """Log deprecation warning"""
        logger.warning(f"DEPRECATED ENDPOINT '{endpoint}' called by user {user_id}")

    def get_router(self):
        router = APIRouter()

        @router.get("/chat_stream")
        async def chat_stream(
            request: Request,
            chats: str = Query(..., description="Compressed chat history in LZString format"),
            conversation_id: str = Query(None, description="Conversation ID for continuing a chat"),
            detected_language: str = Query(None, description="Language detected from speech API (optional)"),
            source_type: str = Query("text", description="Message source: 'text' or 'voice'"),
            audio_duration: float = Query(0, description="Audio duration in seconds (for voice messages)")
        ):
            try:
                # Remove test_vector_content() - debug code that slows production
                
                async def stream_response():
                    try:
                        # Send immediate acknowledgment
                        yield f"data: {json.dumps({'type': 'start'})}\n\n"
                        
                        # Verify user token and get user_id
                        token_data = self.auth.verify_request_token(request)
                        user_id = token_data.get("user_id")

                        # CHECK SUBSCRIPTION AND USAGE LIMITS
                        # Voice messages: quota is already checked & deducted by
                        # /speech/transcribe — skip here to avoid double-deducting.
                        # Text messages: check and later deduct as a query.
                        is_voice_message = source_type == 'voice'
                        if SUBSCRIPTION_FEATURES_ENABLED and not is_voice_message:
                            try:
                                with self.db.get_session() as session:
                                    subscription_service = SubscriptionService(session)
                                    user_uuid = uuid.UUID(user_id)
                                    
                                    query_validation = subscription_service.check_query_limit_for_freemium(user_uuid)
                                    if not query_validation.get('allowed', True):
                                        yield f"data: {json.dumps({'type': 'query_limit_exceeded', 'content': query_validation})}\n\n"
                                        yield f"data: {json.dumps({'type': 'done'})}\n\n"
                                        return
                            except Exception as e:
                                logger.warning(f"Subscription check failed, allowing action: {e}")

                        # Generate conversation_id if not provided
                        if not conversation_id:
                            conversation_id_final = str(uuid.uuid4())
                        else:
                            conversation_id_final = conversation_id

                        # Get or create conversation using ConversationService
                        conversation_id_final, is_new = await self.conversation_service.get_or_create_conversation(
                            user_id=user_id, 
                            conversation_id=conversation_id_final
                        )

                        # Send processing status
                        yield f"data: {json.dumps({'type': 'status', 'message': 'Processing...'})}\n\n"

                        # Decompress and parse chat history
                        decompressor = lzstring.LZString()
                        decompressed_chats = decompressor.decompressFromEncodedURIComponent(chats)
                        json_chats = json.loads(decompressed_chats)
                        
                        # Store the new user message using ConversationService (both Redis and DB)
                        user_message_index, assistant_message_index = await self._reserve_message_indexes(
                            conversation_id_final,
                            user_id,
                        )
                        user_message = {
                            'role': 'user',
                            'content': json_chats[-1]['content'],
                            'message_index': user_message_index,
                            'timestamp': datetime.now().isoformat(),
                            'language': detected_language,
                            'source_type': source_type
                        }
                        
                        await self.conversation_service.store_message(
                            conversation_id=conversation_id_final,
                            user_id=user_id,
                            message=user_message
                        )
                        
                        # Build the chat history
                        chat_history = []
                        for chat in json_chats[:-1]:
                            role = chat.get("role") or chat.get("sender")
                            if role == "user":
                                chat_history.append(HumanMessage(content=chat["content"]))
                            elif role in ("assistant", "bot"):
                                chat_history.append(AIMessage(content=chat["content"]))

                        # Get the question
                        question = json_chats[-1]['content']
                        logger.info(
                            "Received chat question",
                            extra={
                                "conversation_id": conversation_id_final,
                                "question_length": len(question or ""),
                            },
                        )
                        
                        # Use optimal language detection
                        language = get_optimal_language_detection(
                            text=question,
                            speech_detected_language=detected_language
                        )
                        logger.info(
                            "Detected chat language",
                            extra={
                                "conversation_id": conversation_id_final,
                                "language": language,
                            },
                        )

                        # Sanitize the chat history
                        chat_history = self.sanitize_messages(chat_history)
                        
                        # Sanitize the current question
                        question = self.sanitize_text(question)

                        # Check if this is an off-topic message (greeting, thanks, etc.)
                        is_off_topic = self.prompt_templates.is_off_topic_message(question)
                        logger.info(
                            "Off-topic detection completed",
                            extra={
                                "conversation_id": conversation_id_final,
                                "is_off_topic": is_off_topic,
                            },
                        )

                        # Check for referral intent and legal matter flow state
                        referral_intent = None
                        is_referral_request = False
                        is_legal_matter_response = False
                        should_start_referral_flow = False
                        
                        if not is_off_topic:
                            # Check if we previously asked about legal matter
                            legal_matter_asked_key = f"legal_matter_asked:{conversation_id_final}"
                            legal_matter_asked = await self.redis_service.get_key(legal_matter_asked_key)
                            
                            referral_intent = self.prompt_templates.detect_referral_intent(question)
                            is_referral_request = referral_intent['detected'] and referral_intent['confidence'] > 0.5
                            
                            # Debug logging for referral detection
                            logger.info(
                                "Referral detection completed",
                                extra={
                                    "conversation_id": conversation_id_final,
                                    "detected": referral_intent["detected"],
                                    "confidence": referral_intent["confidence"],
                                    "is_referral_request": is_referral_request,
                                    "legal_matter_asked": bool(legal_matter_asked),
                                },
                            )
                            
                            if is_referral_request and not legal_matter_asked:
                                # First time referral request - ask about legal matter
                                logger.info(f"First referral request detected: confidence={referral_intent['confidence']:.2f}, asking about legal matter")
                                
                                # Mark that we've asked about legal matter
                                await self.redis_service.set_key(legal_matter_asked_key, "true", expire=3600)
                                
                                # Store partial referral context
                                try:
                                    referral_context = {
                                        'user_id': user_id,
                                        'conversation_id': conversation_id_final,
                                        'legal_area': referral_intent['legal_area'] or 'general',
                                        'urgency': referral_intent['urgency'],
                                        'original_question': question,
                                        'matched_phrases': referral_intent['specific_phrases'],
                                        'confidence': referral_intent['confidence'],
                                        'legal_matter_asked': True,
                                        'timestamp': datetime.now().isoformat()
                                    }
                                    await self.redis_service.set_key(
                                        f"referral_context:{conversation_id_final}",
                                        json.dumps(referral_context),
                                        expire=REFERRAL_CONTEXT_TTL_SECONDS
                                    )
                                    logger.info(f"Stored partial referral context for conversation {conversation_id_final}")
                                except Exception as e:
                                    logger.warning(f"Failed to store referral context: {e}")
                                    
                            elif legal_matter_asked and not is_referral_request:
                                # User is responding about their legal matter - NOW start the referral flow
                                is_legal_matter_response = True
                                should_start_referral_flow = True
                                logger.info(
                                    "User provided legal matter details",
                                    extra={
                                        "conversation_id": conversation_id_final,
                                        "question_length": len(question or ""),
                                    },
                                )
                                
                                # Update referral context with legal matter details
                                try:
                                    existing_context_data = await self.redis_service.get_key(f"referral_context:{conversation_id_final}")
                                    if existing_context_data:
                                        referral_context = json.loads(existing_context_data)
                                        referral_context['legal_matter_description'] = question
                                        referral_context['ready_for_flow'] = True
                                        
                                        await self.redis_service.set_key(
                                            f"referral_context:{conversation_id_final}",
                                            json.dumps(referral_context),
                                            expire=REFERRAL_CONTEXT_TTL_SECONDS
                                        )
                                        logger.info(f"Updated referral context with legal matter details")
                                except Exception as e:
                                    logger.warning(f"Failed to update referral context: {e}")
                                    
                                # Clean up the flag
                                await self.redis_service.delete_key(legal_matter_asked_key)

                        # Send retrieval status
                        yield f"data: {json.dumps({'type': 'status', 'message': 'Retrieving information...'})}\n\n"

                        # Handle different message types
                        source_references = []
                        retrieval_intent = {}
                        if is_off_topic:
                            # Use simple conversation template without document retrieval
                            template = self.prompt_templates.get_simple_conversation_template(language)
                            context = ""  # No legal context needed
                            retrieved_docs = []
                            logger.info("Using simple conversation template for off-topic message")
                        elif is_referral_request:
                            # Use referral template for initial lawyer referral requests
                            template = self.prompt_templates.get_referral_template(
                                language=language,
                                legal_area=referral_intent['legal_area'],
                                urgency=referral_intent['urgency']
                            )
                            context = ""  # No document context needed for referral
                            retrieved_docs = []
                            logger.info(f"TEMPLATE DEBUG: Using referral template for lawyer request")
                        elif is_legal_matter_response:
                            # Use acknowledgment template for legal matter responses
                            template = self.prompt_templates.get_legal_matter_acknowledgment_template(language)
                            context = ""
                            retrieved_docs = []
                            logger.info(f"Using legal matter acknowledgment template")
                        else:
                            # Legal-aware retrieval for legal questions.
                            retrieved_docs, context, source_references, retrieval_intent = await self.legal_rag_retrieval_with_sources(
                                question=question,
                                k=5,
                                language=language,
                            )
                            
                            # Log retrieval results
                            logger.info(f"Enhanced retrieval results:")
                            logger.info(f"  - Retrieved {len(retrieved_docs)} documents")
                            logger.info(f"  - Total context length: {len(context)} characters")

                            # Get appropriate template for legal questions. Source-aware
                            # retrieval feeds the selector but no longer forces a single
                            # prompt pipeline.
                            template = await self.get_appropriate_template(
                                question,
                                context,
                                language,
                                source_references=source_references,
                                retrieval_intent=retrieval_intent,
                            )

                        # Send generation status
                        yield f"data: {json.dumps({'type': 'status', 'message': 'Generating response...'})}\n\n"

                        # Get response configuration - optimized for speed
                        if self.hallucination_config:
                            response_config = self.hallucination_config.get('response_config', {
                                'model': 'gpt-4.1', 'temperature': 0.1, 'max_tokens': 3000, 'confidence_threshold': 0.5
                            })
                        else:
                            response_config = {'model': 'gpt-4.1', 'temperature': 0.1, 'max_tokens': 3000, 'confidence_threshold': 0.5}
                        
                        # Configure LLM with response settings
                        llm = ChatOpenAI(
                            model_name=response_config.get('model', 'gpt-4.1'), 
                            temperature=response_config.get('temperature', 0.1),
                            max_tokens=response_config.get('max_tokens', 3000),
                            api_key=os.getenv("SECRET_KEY"),
                            streaming=True  # Enable streaming
                        )

                        # Create the chain - different for off-topic, referral, legal matter response, vs legal questions
                        if is_off_topic:
                            # Simple chain without context for off-topic messages
                            chain = (
                                RunnableParallel({
                                    "chat_history": lambda x: x["chat_history"],
                                    "question": lambda x: x["question"]
                                })
                                | template 
                                | llm 
                                | StrOutputParser()
                            )
                        elif is_referral_request or is_legal_matter_response:
                            # Simple chain without context for referral requests and legal matter responses
                            chain = (
                                RunnableParallel({
                                    "chat_history": lambda x: x["chat_history"],
                                    "question": lambda x: x["question"]
                                })
                                | template 
                                | llm 
                                | StrOutputParser()
                            )
                        else:
                            # Full chain with context for legal questions
                            chain = (
                                RunnableParallel({
                                    "context": lambda x: context,
                                    "chat_history": lambda x: x["chat_history"],
                                    "question": lambda x: x["question"]
                                })
                                | template 
                                | llm 
                                | StrOutputParser()
                            )

                        # Stream the response with validation - KEEP ALL EXISTING LOGIC
                        logger.info(
                            "Starting chat generation",
                            extra={
                                "conversation_id": conversation_id_final,
                                "language": language,
                                "chat_history_count": len(chat_history),
                                "question_length": len(question or ""),
                            },
                        )
                        current_response = []
                        
                        async for chunk in chain.astream({"question": question, "chat_history": chat_history}):
                            logger.debug("Streaming chat chunk", extra={"chunk_length": len(chunk or "")})
                            current_response.append(chunk)
                            yield f"data: {json.dumps({'type': 'token', 'content': chunk})}\n\n"
                        
                        # Complete response processing
                        full_response = ''.join(current_response)
                        sources_block = ""
                        if source_references and self.legal_rag_retrieval:
                            sources_block = self.legal_rag_retrieval.format_sources_for_response(source_references)
                            if sources_block and "## Sources" not in full_response:
                                full_response = f"{full_response}{sources_block}"
                                yield f"data: {json.dumps({'type': 'token', 'content': sources_block})}\n\n"

                        # referral_detected must arrive BEFORE end so the frontend can
                        # open the referral flow before the stream is considered done.
                        # This is a Redis read (~5ms) and is the only await before end.
                        if should_start_referral_flow:
                            try:
                                referral_context_data = await self.redis_service.get_key(f"referral_context:{conversation_id_final}")
                                if referral_context_data:
                                    referral_context = json.loads(referral_context_data)
                                    yield f"data: {json.dumps({'type': 'referral_detected', 'context': referral_context})}\n\n"
                            except Exception as e:
                                logger.warning(f"Failed to send referral context: {e}")

                        assistant_message = {
                            'role': 'assistant',
                            'content': full_response,
                            'message_index': assistant_message_index,
                            'timestamp': datetime.now().isoformat(),
                            'language': language,
                            'source_type': 'text',
                            'metadata': {
                                'validation_result': {'is_hallucination': False, 'confidence_score': 0.0},
                                'source_docs': len(retrieved_docs),
                                'context_length': len(context),
                                'sources': json_serializable(source_references),
                                'retrieval_intent': json_serializable(retrieval_intent),
                                'off_topic': is_off_topic,
                                'referral_request': is_referral_request,
                                'referral_intent': json_serializable(referral_intent) if referral_intent else None,
                                'can_receive_feedback': True,
                            },
                        }

                        yield f"data: {json.dumps({'type': 'end', 'can_receive_feedback': True, 'conversation_id': conversation_id_final, 'message_index': assistant_message_index, 'sources': json_serializable(source_references), 'validation': {'is_hallucination': False, 'confidence_score': 0.0}})}\n\n"


                        # All remaining work runs in the background after end is sent.
                        # asyncio.create_task schedules the coroutine and returns immediately;
                        # the task continues even after the generator is exhausted.
                        _captured_response = full_response
                        _captured_conv_id = conversation_id_final
                        _captured_user_id = user_id
                        _captured_language = language
                        _captured_is_voice = is_voice_message
                        _captured_retrieved_docs = retrieved_docs
                        _captured_context = context
                        _captured_source_references = source_references
                        _captured_is_off_topic = is_off_topic
                        _captured_is_referral = is_referral_request
                        _captured_referral_intent = referral_intent
                        _captured_message_index = assistant_message_index
                        _captured_assistant_message = assistant_message
                        _captured_question = question

                        async def _post_stream_tasks():
                            assistant_redis_stored = False
                            try:
                                assistant_redis_stored = await self.redis_service.store_chat_message(
                                    conversation_id=_captured_conv_id,
                                    user_id=_captured_user_id,
                                    message=_captured_assistant_message,
                                )
                            except Exception as _e:
                                logger.warning(f"Failed to store assistant message in Redis after stream end: {_e}")

                            try:
                                await self.conversation_service.store_message_in_db(
                                    conversation_id=_captured_conv_id,
                                    user_id=_captured_user_id,
                                    message=_captured_assistant_message,
                                )
                            except Exception as _e:
                                redis_state = "after Redis success" if assistant_redis_stored else "after Redis failure"
                                logger.warning(f"Failed to persist assistant message {redis_state}: {_e}")

                            if self.legal_rag_retrieval and _captured_source_references:
                                try:
                                    persisted_sources = self.legal_rag_retrieval.persist_response_sources(
                                        conversation_id=_captured_conv_id,
                                        user_id=_captured_user_id,
                                        message_index=_captured_message_index,
                                        sources=_captured_source_references,
                                    )
                                    logger.info(f"Persisted {persisted_sources} chat response sources")
                                except Exception as _e:
                                    logger.warning(f"Failed to persist chat response sources: {_e}")

                            # Start hallucination detection only after the
                            # client has received the final stream event. This
                            # keeps validation embeddings from competing with
                            # feedback-button finalization.
                            if HALLUCINATION_FEATURES_ENABLED and self.hallucination_detector and not _captured_is_off_topic:
                                try:
                                    await self._async_hallucination_validation(
                                        _captured_conv_id,
                                        _captured_user_id,
                                        _captured_context,
                                        _captured_response,
                                        _captured_question,
                                    )
                                except Exception as _e:
                                    logger.warning(f"Background hallucination validation failed: {_e}")

                            # Deduct usage quota (text messages only) after the
                            # client has received the final stream event.
                            if SUBSCRIPTION_FEATURES_ENABLED and not _captured_is_voice:
                                try:
                                    with self.db.get_session() as _sess:
                                        _svc = SubscriptionService(_sess)
                                        _svc.deduct_usage(
                                            user_id=uuid.UUID(_captured_user_id),
                                            action_type=UsageActionType.QUERY,
                                            conversation_id=_captured_conv_id,
                                        )
                                except Exception as _e:
                                    logger.warning(f"Failed to track usage: {_e}")

                        asyncio.create_task(_post_stream_tasks())
                        
                    except Exception as e:
                        logger.error(f"Error in stream_response: {e}")
                        yield f"data: {json.dumps({'type': 'error', 'message': 'An error occurred'})}\n\n"
                    
                return StreamingResponse(
                    stream_response(), 
                    media_type="text/event-stream",
                    headers={
                        'Cache-Control': 'no-cache',
                        'Connection': 'keep-alive',
                        'X-Accel-Buffering': 'no',  # Disable nginx buffering
                    }
                )

            except Exception as e:
                logger.error(f"Error in chat_stream: {e}")
                raise HTTPException(status_code=500, detail=str(e))

        @router.post("/chat_stream")
        async def chat_stream_with_file(
            request: Request,
            chats: str = Form(..., description="Compressed chat history in LZString format"),
            conversation_id: str = Form(None, description="Conversation ID for continuing a chat"),
            detected_language: str = Form(None, description="Language detected from speech API (optional)"),
            source_type: str = Form("file", description="Message source type"),
            file: UploadFile = File(..., description="Image (JPEG, PNG, WebP, GIF) or PDF file to analyze")
        ):
            """
            Chat endpoint with file upload for legal document analysis.
            Supports images (JPEG, PNG, WebP, GIF) via GPT-4o Vision
            and PDFs via text extraction + GPT-4o.
            """
            try:
                # Read file BEFORE entering the generator — FastAPI closes
                # the UploadFile after the endpoint function returns, so it
                # would be unavailable inside the lazily-evaluated generator.
                file_content = await file.read()
                file_content_type = file.content_type
                file_filename = file.filename

                allowed_types = [
                    'image/jpeg', 'image/jpg', 'image/png', 'image/gif', 'image/webp',
                    'application/pdf'
                ]
                if file_content_type not in allowed_types:
                    raise HTTPException(status_code=400, detail="Invalid file type. Supported: JPEG, PNG, WebP, GIF, PDF")

                is_pdf = file_content_type == 'application/pdf'
                max_size = 25 * 1024 * 1024 if is_pdf else 10 * 1024 * 1024
                if len(file_content) > max_size:
                    size_label = "25MB" if is_pdf else "10MB"
                    raise HTTPException(status_code=400, detail=f"File too large. Maximum size is {size_label}.")

                # For PDFs, extract text upfront (CPU-bound, do before generator)
                extracted_pdf_text = None
                pdf_page_count = 0
                if is_pdf:
                    try:
                        import fitz
                        doc = fitz.open(stream=file_content, filetype="pdf")
                        pdf_page_count = len(doc)
                        pages_text = []
                        for page_num in range(pdf_page_count):
                            page = doc.load_page(page_num)
                            pages_text.append(page.get_text())
                        doc.close()
                        extracted_pdf_text = "\n\n".join(pages_text)
                        if not extracted_pdf_text.strip():
                            raise HTTPException(status_code=400, detail="Could not extract text from PDF. The file may be scanned or image-based — try uploading a photo of the pages instead.")
                    except HTTPException:
                        raise
                    except Exception as e:
                        logger.error(f"PDF extraction failed: {e}")
                        raise HTTPException(status_code=400, detail="Failed to read PDF. The file may be corrupted.")

                async def stream_image_response():
                    try:
                        yield f"data: {json.dumps({'type': 'start'})}\n\n"

                        token_data = self.auth.verify_request_token(request)
                        user_id = token_data.get("user_id")

                        # Validate subscription (file/PDF analysis counts as an upload for caps & minutes)
                        if SUBSCRIPTION_FEATURES_ENABLED:
                            try:
                                with self.db.get_session() as session:
                                    subscription_service = SubscriptionService(session)
                                    user_uuid = uuid.UUID(user_id)
                                    can_do, blocked_reason, message = subscription_service.can_perform_action(
                                        user_uuid,
                                        UsageActionType.UPLOAD,
                                        file_size_bytes=len(file_content),
                                        page_count=pdf_page_count if is_pdf else 0,
                                        is_image=not is_pdf,
                                    )
                                    if not can_do:
                                        yield f"data: {json.dumps({'type': 'upload_limit_exceeded', 'content': {'allowed': False, 'reason': blocked_reason.value if blocked_reason else 'upload_blocked', 'message': message}})}\n\n"
                                        yield f"data: {json.dumps({'type': 'done'})}\n\n"
                                        return
                            except Exception as e:
                                logger.warning(f"Subscription check failed, allowing action: {e}")

                        # Get or create conversation
                        if not conversation_id:
                            conversation_id_final = str(uuid.uuid4())
                        else:
                            conversation_id_final = conversation_id

                        conversation_id_final, is_new = await self.conversation_service.get_or_create_conversation(
                            user_id=user_id,
                            conversation_id=conversation_id_final
                        )

                        yield f"data: {json.dumps({'type': 'status', 'message': 'Uploading file...'})}\n\n"

                        # Upload file to S3
                        s3_file_type = "chatbot_document" if is_pdf else "chatbot_image"
                        default_name = "uploaded_document.pdf" if is_pdf else "uploaded_image.jpg"
                        s3_result = self.s3_service.upload_file(
                            file_content=file_content,
                            file_name=file_filename or default_name,
                            content_type=file_content_type,
                            referral_id=f"chatbot/{user_id}",
                            file_type=s3_file_type
                        )

                        file_url = s3_result['url'] if s3_result else None
                        file_s3_key = s3_result['s3_key'] if s3_result else None

                        if not file_url:
                            yield f"data: {json.dumps({'type': 'error', 'message': 'Failed to upload file. Please try again.'})}\n\n"
                            return

                        yield f"data: {json.dumps({'type': 'status', 'message': 'Analyzing document...'})}\n\n"

                        # Decompress and parse chat history
                        decompressor = lzstring.LZString()
                        decompressed_chats = decompressor.decompressFromEncodedURIComponent(chats)
                        json_chats = json.loads(decompressed_chats)

                        question = json_chats[-1]['content'] if json_chats else "Please analyze this legal document."

                        # Detect language
                        language = get_optimal_language_detection(
                            text=question,
                            speech_detected_language=detected_language
                        )

                        # Store user message with file metadata
                        source = 'pdf' if is_pdf else 'image'
                        analysis_type = 'legal_document_pdf' if is_pdf else 'legal_document'
                        user_message_index, assistant_message_index = await self._reserve_message_indexes(
                            conversation_id_final,
                            user_id,
                        )
                        user_message = {
                            'role': 'user',
                            'content': question,
                            'message_index': user_message_index,
                            'timestamp': datetime.now().isoformat(),
                            'language': language,
                            'source_type': source,
                            'image_url': file_url,
                            'image_s3_key': file_s3_key,
                            'image_analysis_type': analysis_type
                        }

                        await self.conversation_service.store_message(
                            conversation_id=conversation_id_final,
                            user_id=user_id,
                            message=user_message
                        )

                        # Build chat history for context
                        chat_history_messages = []
                        for chat in json_chats[:-1]:
                            role = "user" if chat["role"] == "user" else "assistant"
                            chat_history_messages.append({"role": role, "content": chat["content"]})

                        if is_pdf:
                            # PDF path: send extracted text as context to GPT-4o
                            system_prompt = self.prompt_templates.get_pdf_document_analysis_system_prompt(language)

                            # Truncate very long PDFs to fit in context window
                            pdf_text = extracted_pdf_text
                            if len(pdf_text) > 100000:
                                pdf_text = pdf_text[:100000] + "\n\n[... document truncated due to length ...]"

                            messages = [{"role": "system", "content": system_prompt}]
                            messages.extend(chat_history_messages)
                            messages.append({
                                "role": "user",
                                "content": f"{question}\n\n---\n\n**DOCUMENT CONTENT ({pdf_page_count} pages):**\n\n{pdf_text}"
                            })

                            model_name = "gpt-4o"
                        else:
                            # Image path: send image to GPT-4o Vision
                            system_prompt = self.prompt_templates.get_image_document_analysis_system_prompt(language)

                            image_base64 = base64.b64encode(file_content).decode('utf-8')
                            image_data_url = f"data:{file_content_type};base64,{image_base64}"

                            messages = [{"role": "system", "content": system_prompt}]
                            messages.extend(chat_history_messages)
                            messages.append({
                                "role": "user",
                                "content": [
                                    {"type": "text", "text": question},
                                    {"type": "image_url", "image_url": {"url": image_data_url, "detail": "high"}}
                                ]
                            })

                            model_name = "gpt-4o"

                        yield f"data: {json.dumps({'type': 'status', 'message': 'Generating analysis...'})}\n\n"

                        # Stream response from GPT-4o
                        current_response = []
                        stream = self.openai_client.chat.completions.create(
                            model=model_name,
                            messages=messages,
                            max_tokens=4000,
                            temperature=0.1,
                            stream=True
                        )

                        for chunk in stream:
                            if chunk.choices and chunk.choices[0].delta.content:
                                token = chunk.choices[0].delta.content
                                current_response.append(token)
                                yield f"data: {json.dumps({'type': 'token', 'content': token})}\n\n"

                        full_response = ''.join(current_response)

                        assistant_message = {
                            'role': 'assistant',
                            'content': full_response,
                            'message_index': assistant_message_index,
                            'timestamp': datetime.now().isoformat(),
                            'language': language,
                            'source_type': 'text',
                            'metadata': {
                                'source_docs': 0,
                                'context_length': len(pdf_text) if is_pdf else 0,
                                'off_topic': False,
                                'document_analysis': True,
                                'file_type': 'pdf' if is_pdf else 'image',
                                'file_url': file_url,
                                'pdf_page_count': pdf_page_count if is_pdf else None,
                                'can_receive_feedback': True,
                            },
                        }

                        end_data = {
                            'type': 'end',
                            'can_receive_feedback': True,
                            'conversation_id': conversation_id_final,
                            'message_index': assistant_message_index,
                            'file_url': file_url,
                            'file_type': 'pdf' if is_pdf else 'image',
                            'validation': {'is_hallucination': False, 'confidence_score': 0.0}
                        }
                        if is_pdf:
                            end_data['pdf_page_count'] = pdf_page_count
                        yield f"data: {json.dumps(end_data)}\n\n"

                        # All storage and quota work runs in the background after end.
                        _f_response = full_response
                        _f_conv_id = conversation_id_final
                        _f_user_id = user_id
                        _f_language = language
                        _f_is_pdf = is_pdf
                        _f_pdf_text = extracted_pdf_text
                        _f_file_url = file_url
                        _f_pdf_pages = pdf_page_count
                        _f_file_content = file_content
                        _f_file_name = file_filename
                        _f_message_index = assistant_message_index
                        _f_assistant_message = assistant_message

                        async def _post_file_stream_tasks():
                            assistant_redis_stored = False
                            try:
                                assistant_redis_stored = await self.redis_service.store_chat_message(
                                    conversation_id=_f_conv_id,
                                    user_id=_f_user_id,
                                    message=_f_assistant_message,
                                )
                            except Exception as _e:
                                logger.warning(f"Failed to store file assistant message in Redis after stream end: {_e}")

                            try:
                                await self.conversation_service.store_message_in_db(
                                    conversation_id=_f_conv_id,
                                    user_id=_f_user_id,
                                    message=_f_assistant_message,
                                )
                            except Exception as _e:
                                redis_state = "after Redis success" if assistant_redis_stored else "after Redis failure"
                                logger.warning(f"Failed to persist file assistant message {redis_state}: {_e}")

                            # Deduct upload quota after the stream has ended.
                            if SUBSCRIPTION_FEATURES_ENABLED:
                                try:
                                    with self.db.get_session() as _sess:
                                        _svc = SubscriptionService(_sess)
                                        _svc.deduct_usage(
                                            user_id=uuid.UUID(_f_user_id),
                                            action_type=UsageActionType.UPLOAD,
                                            file_size_bytes=len(_f_file_content),
                                            page_count=_f_pdf_pages if _f_is_pdf else 0,
                                            is_image=not _f_is_pdf,
                                            conversation_id=_f_conv_id,
                                            file_name=_f_file_name,
                                        )
                                except Exception as _e:
                                    logger.warning(f"Failed to track upload usage: {_e}")

                        asyncio.create_task(_post_file_stream_tasks())

                    except Exception as e:
                        logger.error(f"Error in stream_image_response: {e}")
                        logger.error(traceback.format_exc())
                        yield f"data: {json.dumps({'type': 'error', 'message': 'An error occurred while analyzing the document'})}\n\n"

                return StreamingResponse(
                    stream_image_response(),
                    media_type="text/event-stream",
                    headers={
                        'Cache-Control': 'no-cache',
                        'Connection': 'keep-alive',
                        'X-Accel-Buffering': 'no',
                    }
                )

            except Exception as e:
                logger.error(f"Error in chat_stream_with_image: {e}")
                raise HTTPException(status_code=500, detail=str(e))

        @router.get("/hallucination_stats")
        async def get_hallucination_stats(
            request: Request,
            days: int = Query(7, description="Number of days to analyze")
        ):
            """Get hallucination detection statistics"""
            try:
                # Verify admin token (you might want to implement admin authentication)
                token_data = self.auth.verify_request_token(request)
                
                if not self.hallucination_analytics:
                    return JSONResponse(content={'error': 'Hallucination analytics not available'})
                
                stats = await self.hallucination_analytics.get_hallucination_stats(days)
                # Ensure stats are JSON serializable
                serializable_stats = json_serializable(stats)
                return JSONResponse(content=serializable_stats)
                
            except Exception as e:
                logger.error(f"Error getting hallucination stats: {e}")
                raise HTTPException(status_code=500, detail=str(e))

        @router.get("/recent_validations")
        async def get_recent_validations(
            request: Request,
            limit: int = Query(50, description="Number of recent validations to retrieve")
        ):
            """Get recent validation results for analysis"""
            try:
                # Verify admin token
                token_data = self.auth.verify_request_token(request)
                
                if not self.hallucination_analytics:
                    return JSONResponse(content={'error': 'Hallucination analytics not available'})
                
                validations = await self.hallucination_analytics.get_recent_validations(limit)
                # Ensure validations are JSON serializable
                serializable_validations = json_serializable(validations)
                return JSONResponse(content={'validations': serializable_validations})
                
            except Exception as e:
                logger.error(f"Error getting recent validations: {e}")
                raise HTTPException(status_code=500, detail=str(e))
            
        # DEPRECATED ENDPOINT - Mark for removal
        @router.get(
            "/conversation_history/{conversation_id}",
            deprecated=True,
            summary="[DEPRECATED] Get conversation history from Redis",
            description="""
            ⚠️ **DEPRECATED**: This endpoint is deprecated and will be removed in v2.0.0.
            
            **Migration Path:**
            - Use `GET /conversations/{conversation_id}/history` instead
            - The new endpoint provides persistent storage and better performance
            
            **Removal Date:**  2026
            **Current Version:** v1.x (deprecated)
            **Replacement:** GET /conversations/{conversation_id}/history
            """,
            response_description="Conversation history with deprecation warning",
            tags=["Chat (Deprecated)"]
        )
        async def get_history_deprecated(
            request: Request,
            conversation_id: str,
            limit: int = Query(50, description="Number of messages to retrieve")
        ):
            """
            ⚠️ DEPRECATED - Use GET /conversations/{conversation_id}/history instead
            """
            try:
                # Verify user token
                token_data = self.auth.verify_request_token(request)
                user_id = token_data.get("user_id")
                
                # Log deprecation usage
                self._log_deprecation_warning("conversation_history_redis", user_id)
                
                # Get conversation history from Redis (legacy behavior)
                history = await self.redis_service.get_chat_history(conversation_id, limit)
                
                # Fetch feedback for messages from PostgreSQL
                feedback_map = {}
                if self.db:
                    with self.db.get_session() as session:
                        from services.models import ChatResponseFeedback
                        feedback_records = session.query(ChatResponseFeedback).filter(
                            ChatResponseFeedback.conversation_id == conversation_id,
                            ChatResponseFeedback.user_id == uuid.UUID(user_id)
                        ).all()
                        
                        for feedback in feedback_records:
                            feedback_map[feedback.message_index] = {
                                'feedback_id': str(feedback.id),
                                'feedback_type': feedback.feedback_type,
                                'thumbs_down_reason': feedback.thumbs_down_reason,
                                'created_at': feedback.created_at.isoformat(),
                                'updated_at': feedback.updated_at.isoformat()
                            }
                
                # Merge feedback into messages
                for message in history:
                    message_index = message.get('message_index')
                    if message_index in feedback_map:
                        message['feedback'] = feedback_map[message_index]
                
                # Add deprecation headers and warning
                response_data = {
                    'conversation_id': conversation_id,
                    'messages': history,
                    'total': len(history),
                    '_deprecation_warning': {
                        'message': 'This endpoint is deprecated and will be removed in v2.0.0',
                        'removal_date': '2025-09-05',
                        'replacement_endpoint': f'/conversations/{conversation_id}/history'
                    }
                }
                
                # Create response with deprecation headers
                response = JSONResponse(content=response_data)
                response.headers["X-Deprecated"] = "true"
                response.headers["X-Deprecation-Date"] = "2025-09-05"
                response.headers["X-Replacement-Endpoint"] = f"/conversations/{conversation_id}/history"
                response.headers["Sunset"] = "Wed, 01 Mar 2024 00:00:00 GMT"
                
                return response
                
            except Exception as e:
                logger.error(f"Error in deprecated conversation_history endpoint: {e}")
                raise HTTPException(status_code=500, detail=str(e))

        # NEW PERSISTENT ENDPOINTS (These replace the deprecated ones)
        @router.get(
            "/conversations",
            summary="Get user's conversation list",
            description="Get a list of all conversations for the authenticated user with persistent storage",
            response_description="List of user conversations",
            tags=["Conversations"]
        )
        async def get_user_conversations(
            request: Request,
            limit: int = Query(20, description="Number of conversations to retrieve"),
            offset: int = Query(0, description="Offset for pagination")
        ):
            """Get user's conversation history with persistent storage"""
            try:
                token_data = self.auth.verify_request_token(request)
                user_id = token_data.get("user_id")
                
                # Get conversations from the conversation service
                conversations = await self.conversation_service.get_user_conversations(
                    user_id=user_id, 
                    limit=limit + offset  # Get extra to handle offset
                )
                
                # Apply offset for pagination
                if offset > 0:
                    conversations = conversations[offset:]
                
                # Limit results after offset
                conversations = conversations[:limit]
                
                return JSONResponse(content={
                    'conversations': conversations,
                    'total': len(conversations),
                    'limit': limit,
                    'offset': offset,
                    'has_more': len(conversations) == limit  # Indicate if there might be more
                })
                
            except Exception as e:
                logger.error(f"Error getting conversations: {e}")
                raise HTTPException(status_code=500, detail=str(e))

        @router.get(
            "/conversations/{conversation_id}/history",
            summary="Get conversation history with persistence",
            description="Get message history for a specific conversation with persistent storage support",
            response_description="Conversation messages with metadata",
            tags=["Conversations"]
        )
        async def get_conversation_history_persistent(
            conversation_id: str,
            request: Request,
            limit: int = Query(50, description="Number of messages to retrieve"),
            offset: int = Query(0, description="Offset for pagination")
        ):
            """Get history for a specific conversation with persistent storage"""
            try:
                token_data = self.auth.verify_request_token(request)
                user_id = token_data.get("user_id")
                
                # Try to get from Redis first (fast path)
                redis_history = await self.redis_service.get_chat_history(conversation_id, limit)
                
                if redis_history:
                    # Apply offset if needed
                    if offset > 0:
                        redis_history = redis_history[offset:]
                    
                    # Fetch feedback for messages from PostgreSQL
                    feedback_map = {}
                    if self.db:
                        with self.db.get_session() as session:
                            from services.models import ChatResponseFeedback
                            feedback_records = session.query(ChatResponseFeedback).filter(
                                ChatResponseFeedback.conversation_id == conversation_id,
                                ChatResponseFeedback.user_id == uuid.UUID(user_id)
                            ).all()
                            
                            for feedback in feedback_records:
                                feedback_map[feedback.message_index] = {
                                    'feedback_id': str(feedback.id),
                                    'feedback_type': feedback.feedback_type,
                                    'thumbs_down_reason': feedback.thumbs_down_reason,
                                    'created_at': feedback.created_at.isoformat(),
                                    'updated_at': feedback.updated_at.isoformat()
                                }
                    
                    # Merge feedback into messages
                    for message in redis_history:
                        message_index = message.get('message_index')
                        if message_index in feedback_map:
                            message['feedback'] = feedback_map[message_index]

                    return JSONResponse(content={
                        'conversation_id': conversation_id,
                        'messages': redis_history,
                        'total': len(redis_history),
                        'source': 'redis',
                        'limit': limit,
                        'offset': offset
                    })
                else:
                    # Instead of the TODO, should use:
                    history = await self.conversation_service.get_conversation_history(
                        conversation_id, user_id, limit
                    )
                    return JSONResponse(content={
                        'conversation_id': conversation_id,
                        'messages': history,
                        'total': len(history),
                        'source': 'database',
                        'limit': limit,
                        'offset': offset
                    })
                
            except Exception as e:
                logger.error(f"Error getting conversation history: {e}")
                raise HTTPException(status_code=500, detail=str(e))

        @router.delete(
            "/conversations/{conversation_id}",
            summary="Delete conversation",
            description="Soft delete a conversation and its messages",
            tags=["Conversations"]
        )
        async def delete_conversation(
            conversation_id: str,
            request: Request
        ):
            """Delete a conversation"""
            try:
                token_data = self.auth.verify_request_token(request)
                user_id = token_data.get("user_id")
                
                # For now, just clear from Redis
                success = await self.redis_service.delete_key(f"chat:{conversation_id}")
                
                if success:
                    return JSONResponse(content={
                        'message': 'Conversation deleted successfully',
                        'conversation_id': conversation_id
                    })
                else:
                    raise HTTPException(status_code=404, detail="Conversation not found")
                
            except Exception as e:
                logger.error(f"Error deleting conversation: {e}")
                raise HTTPException(status_code=500, detail=str(e))

        # Add deprecation info endpoint
        @router.get(
            "/deprecation-info",
            summary="API Deprecation Information",
            description="Get information about deprecated endpoints and migration paths",
            tags=["API Info"]
        )
        async def get_deprecation_info():
            """Get information about deprecated API endpoints"""
            return JSONResponse(content={
                'deprecated_endpoints': [
                    {
                        'endpoint': 'GET /conversation_history/{conversation_id}',
                        'deprecation_date': '2026',
                        'removal_date': '2026',
                        'replacement': 'GET /conversations/{conversation_id}/history',
                        'reason': 'Moving to persistent storage with better performance',
                        'migration_guide': {
                            'steps': [
                                'Update your client to use the new endpoint URL',
                                'Handle the new response format with pagination',
                                'Update error handling for the new endpoint'
                            ],
                            'breaking_changes': [
                                'Response format includes pagination metadata',
                                'Error codes may differ',
                                'Rate limiting may be different'
                            ]
                        }
                    }
                ],
                'migration_deadline': '2026',
                'support_contact': 'info@lawneeds.com'
            })

        return router
