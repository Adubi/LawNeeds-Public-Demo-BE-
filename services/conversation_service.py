import logging
import json
import uuid
from typing import List, Dict, Optional, Tuple
from datetime import datetime, timezone, timedelta
import asyncio
from sqlalchemy.orm import Session
from sqlalchemy import desc, and_, or_
from services.models import Conversation, ChatMessage, User
from services.redis_service import RedisService
from services.language_detection import get_openai_client

logger = logging.getLogger(__name__)

class ConversationService:
    """
    Service for managing persistent conversation history across user sessions
    """
    
    def __init__(self, db, redis_service: RedisService):
        self.db = db
        self.redis_service = redis_service
        
        # Configuration
        self.redis_ttl = 3600 * 6  # 6 hours in Redis (shorter than before)
        self.max_conversations_per_user = 50  # Limit to prevent DB bloat
        self.auto_title_threshold = 3  # Generate title after 3 messages
    
    async def get_or_create_conversation(self, user_id: str, conversation_id: Optional[str] = None) -> Tuple[str, bool]:
        """
        Get existing conversation or create new one
        Returns: (conversation_id, is_new)
        """
        try:
            with self.db.get_session() as session:
                # First verify user exists
                user = session.query(User).filter(User.id == uuid.UUID(user_id)).first()
                if not user:
                    logger.error(f"User {user_id} not found in database")
                    # Fallback to UUID for Redis-only operation
                    return str(uuid.uuid4()), True
                
                if conversation_id:
                    # Try to find existing conversation
                    conversation = session.query(Conversation).filter(
                        and_(
                            Conversation.id == uuid.UUID(conversation_id),
                            Conversation.user_id == uuid.UUID(user_id),
                            Conversation.is_active == True
                        )
                    ).first()
                    
                    if conversation:
                        # Update last activity
                        conversation.last_activity = datetime.now(timezone.utc)
                        session.commit()
                        return str(conversation.id), False
                
                # Create new conversation
                conversation = Conversation(
                    user_id=uuid.UUID(user_id),
                    conversation_type='general',
                    last_activity=datetime.now(timezone.utc)
                )
                session.add(conversation)
                session.commit()
                
                logger.info(f"Created new conversation {conversation.id} for user {user_id}")
                return str(conversation.id), True
                
        except Exception as e:
            logger.error(f"Error in get_or_create_conversation: {e}")
            # Fallback to UUID if DB fails
            return str(uuid.uuid4()), True
    
    async def store_message(self, conversation_id: str, user_id: str, message: Dict) -> bool:
        """
        Store message in both Redis (for speed) and Database (for persistence)
        """
        try:
            # Store in Redis first for immediate access
            redis_success = await self.redis_service.store_chat_message(
                conversation_id=conversation_id,
                user_id=user_id,
                message=message
            )
            
            # Store in database for persistence
            db_success = await self._store_message_in_db(conversation_id, user_id, message)
            
            # Update conversation metadata if needed
            if message.get('role') == 'user':
                await self._update_conversation_metadata(conversation_id, message.get('content', ''))
            
            return redis_success or db_success  # Success if either works
            
        except Exception as e:
            logger.error(f"Error storing message: {e}")
            return False

    async def store_message_in_db(self, conversation_id: str, user_id: str, message: Dict) -> bool:
        """Store a message in the persistent database only."""
        return await self._store_message_in_db(conversation_id, user_id, message)
    
    async def _store_message_in_db(self, conversation_id: str, user_id: str, message: Dict) -> bool:
        """Store message in database"""
        try:
            with self.db.get_session() as session:
                # Verify both user and conversation exist
                user = session.query(User).filter(User.id == uuid.UUID(user_id)).first()
                if not user:
                    logger.warning(f"User {user_id} not found, skipping DB storage")
                    return False
                
                conversation = session.query(Conversation).filter(
                    and_(
                        Conversation.id == uuid.UUID(conversation_id),
                        Conversation.user_id == uuid.UUID(user_id)
                    )
                ).first()
                
                if not conversation:
                    logger.warning(f"Conversation {conversation_id} not found for user {user_id}, skipping DB storage")
                    return False
                
                # Create chat message
                chat_message = ChatMessage(
                    conversation_id=uuid.UUID(conversation_id),
                    user_id=uuid.UUID(user_id),
                    role=message.get('role', 'user'),
                    content=message.get('content', ''),
                    message_index=message.get('message_index', 0),
                    language=message.get('language'),
                    source_type=message.get('source_type', 'text'),
                    image_url=message.get('image_url'),
                    image_s3_key=message.get('image_s3_key'),
                    image_analysis_type=message.get('image_analysis_type'),
                    response_metadata=message.get('metadata')
                )
                
                session.add(chat_message)
                
                # Update conversation stats
                conversation.message_count += 1
                conversation.last_activity = datetime.now(timezone.utc)
                
                session.commit()
                return True
                
        except Exception as e:
            logger.error(f"Error storing message in DB: {e}")
            return False
    
    async def get_conversation_history(self, conversation_id: str, user_id: str, limit: int = 50) -> List[Dict]:
        """
        Get conversation history from Redis first, fallback to database
        """
        try:
            # Try Redis first (faster)
            redis_history = await self.redis_service.get_chat_history(conversation_id, limit)
            
            if redis_history:
                logger.info(f"Retrieved {len(redis_history)} messages from Redis for conversation {conversation_id}")
                return redis_history
            
            # Fallback to database
            logger.info(f"Redis empty, falling back to database for conversation {conversation_id}")
            return await self._get_history_from_db(conversation_id, user_id, limit)
            
        except Exception as e:
            logger.error(f"Error getting conversation history: {e}")
            return []
    
    async def _get_history_from_db(self, conversation_id: str, user_id: str, limit: int) -> List[Dict]:
        """Get conversation history from database"""
        try:
            with self.db.get_session() as session:
                messages = session.query(ChatMessage).filter(
                    and_(
                        ChatMessage.conversation_id == uuid.UUID(conversation_id),
                        ChatMessage.user_id == uuid.UUID(user_id)
                    )
                ).order_by(ChatMessage.message_index.asc()).limit(limit).all()
                
                history = []
                for msg in messages:
                    msg_data = {
                        'role': msg.role,
                        'content': msg.content,
                        'message_index': msg.message_index,
                        'timestamp': msg.created_at.isoformat(),
                        'user_id': str(msg.user_id),
                        'language': msg.language,
                        'source_type': msg.source_type,
                        'metadata': msg.response_metadata
                    }
                    if msg.image_url:
                        msg_data['image_url'] = msg.image_url
                        msg_data['image_s3_key'] = msg.image_s3_key
                        msg_data['image_analysis_type'] = msg.image_analysis_type
                    history.append(msg_data)
                
                # Reload into Redis for future requests
                if history:
                    await self._reload_redis_from_db(conversation_id, history)
                
                return history
                
        except Exception as e:
            logger.error(f"Error getting history from DB: {e}")
            return []
    
    async def _reload_redis_from_db(self, conversation_id: str, messages: List[Dict]):
        """Reload Redis cache from database messages"""
        try:
            # Clear existing Redis data for this conversation
            await self.redis_service.delete_key(f"chat:{conversation_id}")
            
            # Reload messages into Redis
            for message in messages:
                redis_key = f"chat:{conversation_id}"
                await self.redis_service.redis.rpush(redis_key, json.dumps(message))
            
            # Set expiration
            await self.redis_service.redis.expire(f"chat:{conversation_id}", self.redis_ttl)
            
            logger.info(f"Reloaded {len(messages)} messages into Redis for conversation {conversation_id}")
            
        except Exception as e:
            logger.warning(f"Failed to reload Redis cache: {e}")
    
    async def get_user_conversations(self, user_id: str, limit: int = 20) -> List[Dict]:
        """Get list of user's conversations"""
        try:
            with self.db.get_session() as session:
                conversations = session.query(Conversation).filter(
                    and_(
                        Conversation.user_id == uuid.UUID(user_id),
                        Conversation.is_active == True
                    )
                ).order_by(desc(Conversation.last_activity)).limit(limit).all()
                
                conversation_list = []
                for conv in conversations:
                    conversation_list.append({
                        'id': str(conv.id),
                        'title': conv.title or self._generate_default_title(conv.first_message),
                        'last_activity': conv.last_activity.isoformat(),
                        'message_count': conv.message_count,
                        'conversation_type': conv.conversation_type,
                        'first_message': conv.first_message[:100] + '...' if conv.first_message and len(conv.first_message) > 100 else conv.first_message
                    })
                
                return conversation_list
                
        except Exception as e:
            logger.error(f"Error getting user conversations: {e}")
            return []
    
    async def _update_conversation_metadata(self, conversation_id: str, message_content: str):
        """Update conversation metadata like title and first message"""
        try:
            with self.db.get_session() as session:
                conversation = session.query(Conversation).filter(
                    Conversation.id == uuid.UUID(conversation_id)
                ).first()
                
                if conversation:
                    # Set first message if not set
                    if not conversation.first_message:
                        conversation.first_message = message_content[:500]  # Truncate long messages
                    
                    # Generate title after threshold is reached
                    if (not conversation.title and 
                        conversation.message_count >= self.auto_title_threshold):
                        conversation.title = await self._generate_conversation_title(
                            conversation.first_message or message_content
                        )
                    
                    session.commit()
                    
        except Exception as e:
            logger.warning(f"Error updating conversation metadata: {e}")

    async def _generate_conversation_title(self, first_message: str) -> str:
        """Generate a conversation title using GPT-3.5 with rule-based fallback"""
        try:
            message_text = (first_message or "").strip()
            if not message_text:
                return "Legal Consultation"
            
            # Try OpenAI (via shared client) for smarter title generation
            client = get_openai_client()
            if client:
                system_prompt = (
                    "You are a legal assistant. Generate a concise, professional title (3-6 words) "
                    "that captures the main legal topic from the user's message. "
                    "Only return the title, nothing else."
                )
                user_prompt = f"Generate a title for: {message_text[:300]}"
                
                loop = asyncio.get_event_loop()
                response = await loop.run_in_executor(
                    None,
                    lambda: client.chat.completions.create(
                        model="gpt-3.5-turbo",
                        messages=[
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_prompt}
                        ],
                        max_tokens=20,
                        temperature=0.3
                    )
                )
                ai_title = response.choices[0].message.content.strip().strip('"').strip("'")
                # Basic validation
                if 3 <= len(ai_title) <= 60:
                    return ai_title
            
            # Fallback to rule-based generation
            return self._generate_rule_based_title(first_message)
        except Exception as e:
            logger.warning(f"Error generating title: {e}")
            return self._generate_rule_based_title(first_message)

    def _generate_rule_based_title(self, first_message: str) -> str:
        """Rule-based fallback for title generation"""
        try:
            message = (first_message or "").lower().strip()
            if any(term in message for term in ['immigration', 'visa', 'residency']):
                return "Immigration Inquiry"
            elif any(term in message for term in ['employment', 'workplace', 'job']):
                return "Employment Law"
            elif any(term in message for term in ['family', 'divorce', 'custody']):
                return "Family Law"
            elif any(term in message for term in ['property', 'real estate', 'landlord']):
                return "Property Law"
            elif any(term in message for term in ['criminal', 'police', 'charge']):
                return "Criminal Law"
            elif any(term in message for term in ['contract', 'agreement', 'business']):
                return "Contract Law"
            else:
                words = (first_message or "").split()[:4]
                return ' '.join(words).title() + ('...' if len((first_message or "").split()) > 4 else '')
        except Exception:
            return "Legal Consultation"
    
    def _generate_default_title(self, first_message: Optional[str]) -> str:
        """Generate default title for display"""
        if not first_message:
            return "New Conversation"
        
        words = first_message.split()[:4]
        return ' '.join(words) + ('...' if len(first_message.split()) > 4 else '')
    
    async def delete_conversation(self, conversation_id: str, user_id: str) -> bool:
        """Delete a conversation (soft delete)"""
        try:
            with self.db.get_session() as session:
                conversation = session.query(Conversation).filter(
                    and_(
                        Conversation.id == uuid.UUID(conversation_id),
                        Conversation.user_id == uuid.UUID(user_id)
                    )
                ).first()
                
                if conversation:
                    conversation.is_active = False
                    session.commit()
                    
                    # Also clear from Redis
                    await self.redis_service.delete_key(f"chat:{conversation_id}")
                    
                    return True
                
        except Exception as e:
            logger.error(f"Error deleting conversation: {e}")
            
        return False
    
    async def cleanup_old_conversations(self, days: int = 90) -> int:
        """Cleanup very old conversations"""
        try:
            cutoff_date = datetime.now(timezone.utc) - timedelta(days=days)
            
            with self.db.get_session() as session:
                old_conversations = session.query(Conversation).filter(
                    Conversation.last_activity < cutoff_date
                ).all()
                
                count = 0
                for conv in old_conversations:
                    conv.is_active = False
                    count += 1
                
                session.commit()
                logger.info(f"Cleaned up {count} old conversations")
                return count
                
        except Exception as e:
            logger.error(f"Error cleaning up conversations: {e}")
            return 0
