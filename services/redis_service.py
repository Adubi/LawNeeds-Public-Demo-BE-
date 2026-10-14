import json
import redis.asyncio as redis
import logging
from typing import List, Dict, Optional
from datetime import datetime, timezone
import os
import ssl

logger = logging.getLogger(__name__)

class RedisService:
    def __init__(self):
        try:
            redis_url = os.getenv('REDIS_URL', 'redis://localhost:6379')
            redis_password = os.getenv('REDIS_PASSWORD')

            # Example: handle SSL if your host requires TLS
            if redis_url.startswith("rediss://"):
                # Keep scheme as rediss://
                scheme_prefix = "rediss://"
                no_scheme = redis_url.removeprefix(scheme_prefix)
                # Insert password
                if redis_password:
                    redis_url = f"{scheme_prefix}:{redis_password}@{no_scheme}"
            else:
                # Default to redis://
                scheme_prefix = "redis://"
                no_scheme = redis_url.removeprefix(scheme_prefix)
                if redis_password:
                    redis_url = f"{scheme_prefix}:{redis_password}@{no_scheme}"

            logger.info("Redis URL configured")

            # If using SSL, configure SSL parameters
            self.redis = redis.from_url(
                redis_url,
                decode_responses=True
            )

            logger.info("Redis service initialized")

        except Exception as e:
            logger.error(f"Failed to initialize Redis service: {e}")
            raise

    async def test_connection(self) -> bool:
        """Test the Redis connection"""
        try:
            await self.redis.ping()
            logger.info("Successfully connected to Redis")
            return True
        except redis.ConnectionError as e:
            logger.error(f"Redis connection error: {e}")
            return False
        except Exception as e:
            logger.error(f"Unexpected Redis error: {e}")
            return False

    async def store_chat_message(self, conversation_id: str, user_id: str, message: Dict) -> bool:
        """Store a chat message in Redis"""
        try:
            # Create a message with timestamp
            message_with_metadata = {
                **message,
                'user_id': user_id,
                'timestamp': datetime.now(timezone.utc).isoformat()
            }
            
            # Store in Redis list
            key = f"chat:{conversation_id}"
            await self.redis.rpush(key, json.dumps(message_with_metadata))
            
            # Set expiration (e.g., 24 hours)
            await self.redis.expire(key, 60 * 60 * 24)
            
            return True
        except redis.RedisError as e:
            logger.error(f"Redis error while storing message: {e}")
            return False
        except Exception as e:
            logger.error(f"Error storing chat message: {e}")
            return False

    @staticmethod
    def inbox_events_channel(referral_id: str) -> str:
        """Redis pub/sub channel for real-time inbox events."""
        return f"inbox:{referral_id}:events"

    async def publish_inbox_event(
        self,
        referral_id: str,
        event: Dict,
        exclude_user_id: Optional[str] = None,
    ) -> bool:
        """Publish an inbox event for WebSocket subscribers."""
        try:
            if self.redis is None:
                return False

            envelope = {
                "event": event,
                "exclude_user_id": exclude_user_id,
            }
            await self.redis.publish(
                self.inbox_events_channel(referral_id),
                json.dumps(envelope),
            )
            return True
        except redis.RedisError as e:
            logger.error(f"Redis error while publishing inbox event: {e}")
            return False
        except Exception as e:
            logger.error(f"Error publishing inbox event: {e}")
            return False

    async def get_chat_history(self, conversation_id: str, limit: int = 50) -> List[Dict]:
        """Retrieve chat history from Redis"""
        try:
            key = f"chat:{conversation_id}"
            # Get all messages (or up to limit) from newest to oldest
            messages = await self.redis.lrange(key, -limit, -1)
            
            # Parse JSON strings back to dictionaries
            return [json.loads(msg) for msg in messages]
        except redis.RedisError as e:
            logger.error(f"Redis error while retrieving history: {e}")
            return []
        except json.JSONDecodeError as e:
            logger.error(f"Error decoding message from Redis: {e}")
            return []
        except Exception as e:
            logger.error(f"Error retrieving chat history: {e}")
            return []

    async def delete_chat_history(self, conversation_id: str) -> bool:
        """Delete a conversation's chat history"""
        try:
            key = f"chat:{conversation_id}"
            await self.redis.delete(key)
            return True
        except redis.RedisError as e:
            logger.error(f"Redis error while deleting history: {e}")
            return False
        except Exception as e:
            logger.error(f"Error deleting chat history: {e}")
            return False

    async def get_stats(self) -> Dict:
        """Get Redis statistics"""
        try:
            info = await self.redis.info()
            return {
                "connected_clients": info.get("connected_clients", 0),
                "used_memory_human": info.get("used_memory_human", "0B"),
                "total_connections_received": info.get("total_connections_received", 0),
                "total_commands_processed": info.get("total_commands_processed", 0)
            }
        except redis.RedisError as e:
            logger.error(f"Redis error while getting stats: {e}")
            return {}
        except Exception as e:
            logger.error(f"Error getting Redis stats: {e}")
            return {}

    async def cleanup_old_chats(self, days: int = 7) -> int:
        """Cleanup chat histories older than specified days"""
        try:
            pattern = "chat:*"
            deleted = 0
            async for key in self.redis.scan_iter(match=pattern):
                # Check the timestamp of the last message
                messages = await self.get_chat_history(key.replace("chat:", ""), limit=1)
                if messages and messages[0].get("timestamp"):
                    last_message = datetime.fromisoformat(messages[0]["timestamp"])
                    if (datetime.now(timezone.utc) - last_message).days > days:
                        await self.redis.delete(key)
                        deleted += 1
            return deleted
        except redis.RedisError as e:
            logger.error(f"Redis error during cleanup: {e}")
            return 0
        except Exception as e:
            logger.error(f"Error cleaning up old chats: {e}")
            return 0

    async def store_feedback(self, conversation_id: str, user_id: str, feedback_data: Dict) -> bool:
        """Store chat response feedback temporarily in Redis"""
        try:
            # Create feedback entry with metadata
            feedback_with_metadata = {
                **feedback_data,
                'user_id': user_id,
                'timestamp': datetime.now(timezone.utc).isoformat()
            }
            
            # Store in Redis with conversation-specific key
            key = f"feedback:{conversation_id}:{feedback_data.get('message_index', 0)}"
            await self.redis.setex(
                key, 
                60 * 60 * 24 * 7,  # 7 days expiration
                json.dumps(feedback_with_metadata)
            )
            
            logger.info(f"Feedback stored in Redis for conversation {conversation_id}")
            return True
        except redis.RedisError as e:
            logger.error(f"Redis error while storing feedback: {e}")
            return False
        except Exception as e:
            logger.error(f"Error storing feedback: {e}")
            return False

    async def get_conversation_feedback(self, conversation_id: str) -> List[Dict]:
        """Retrieve all feedback for a conversation from Redis"""
        try:
            pattern = f"feedback:{conversation_id}:*"
            feedback_list = []
            
            async for key in self.redis.scan_iter(match=pattern):
                feedback_data = await self.redis.get(key)
                if feedback_data:
                    try:
                        parsed_feedback = json.loads(feedback_data)
                        feedback_list.append(parsed_feedback)
                    except json.JSONDecodeError:
                        logger.warning(f"Failed to parse feedback data for key: {key}")
            
            # Sort by message_index
            feedback_list.sort(key=lambda x: x.get('message_index', 0))
            return feedback_list
        except redis.RedisError as e:
            logger.error(f"Redis error while retrieving feedback: {e}")
            return []
        except Exception as e:
            logger.error(f"Error retrieving conversation feedback: {e}")
            return []

    async def delete_feedback(self, conversation_id: str, message_index: int = None) -> bool:
        """Delete feedback for a specific message or entire conversation"""
        try:
            if message_index is not None:
                # Delete specific message feedback
                key = f"feedback:{conversation_id}:{message_index}"
                await self.redis.delete(key)
            else:
                # Delete all feedback for conversation
                pattern = f"feedback:{conversation_id}:*"
                deleted_count = 0
                async for key in self.redis.scan_iter(match=pattern):
                    await self.redis.delete(key)
                    deleted_count += 1
                logger.info(f"Deleted {deleted_count} feedback entries for conversation {conversation_id}")
            
            return True
        except redis.RedisError as e:
            logger.error(f"Redis error while deleting feedback: {e}")
            return False
        except Exception as e:
            logger.error(f"Error deleting feedback: {e}")
            return False

    async def set_key(self, key: str, value: str, expire: int = None) -> bool:
        """Set a key-value pair in Redis with optional expiration"""
        try:
            if self.redis is None:
                return False
            if expire:
                await self.redis.setex(key, expire, value)
            else:
                await self.redis.set(key, value)
            return True
        except redis.RedisError as e:
            logger.error(f"Redis error while setting key {key}: {e}")
            return False
        except Exception as e:
            logger.error(f"Error setting key {key}: {e}")
            return False

    async def get_key(self, key: str) -> Optional[str]:
        """Get a value from Redis by key"""
        try:
            if self.redis is None:
                return None
            value = await self.redis.get(key)
            return value
        except redis.RedisError as e:
            logger.error(f"Redis error while getting key {key}: {e}")
            return None
        except Exception as e:
            logger.error(f"Error getting key {key}: {e}")
            return None

    async def delete_key(self, key: str) -> bool:
        """Delete a key from Redis"""
        try:
            if self.redis is None:
                return False
            await self.redis.delete(key)
            return True
        except redis.RedisError as e:
            logger.error(f"Redis error while deleting key {key}: {e}")
            return False
        except Exception as e:
            logger.error(f"Error deleting key {key}: {e}")
            return False

    async def get_keys_by_pattern(self, pattern: str) -> List[str]:
        """Get all keys matching a pattern"""
        try:
            if self.redis is None:
                return []
            
            keys = []
            async for key in self.redis.scan_iter(match=pattern):
                keys.append(key)
            return keys
        except redis.RedisError as e:
            logger.error(f"Redis error while getting keys by pattern {pattern}: {e}")
            return []
        except Exception as e:
            logger.error(f"Error getting keys by pattern {pattern}: {e}")
            return []

    async def get_multiple_keys(self, keys: List[str]) -> Dict[str, Optional[str]]:
        """Get multiple keys in a single operation"""
        try:
            if self.redis is None or not keys:
                return {}
            
            values = await self.redis.mget(keys)
            return dict(zip(keys, values))
        except redis.RedisError as e:
            logger.error(f"Redis error while getting multiple keys: {e}")
            return {}
        except Exception as e:
            logger.error(f"Error getting multiple keys: {e}")
            return {}

    async def set_multiple_keys(self, key_value_pairs: Dict[str, str], expire: Optional[int] = None) -> bool:
        """Set multiple keys in a single operation"""
        try:
            if self.redis is None or not key_value_pairs:
                return False
            
            # Use pipeline for better performance
            async with self.redis.pipeline() as pipe:
                await pipe.mset(key_value_pairs)
                
                if expire:
                    for key in key_value_pairs.keys():
                        await pipe.expire(key, expire)
                
                await pipe.execute()
            
            return True
        except redis.RedisError as e:
            logger.error(f"Redis error while setting multiple keys: {e}")
            return False
        except Exception as e:
            logger.error(f"Error setting multiple keys: {e}")
            return False

    async def delete_key(self, key: str) -> bool:
        """Delete a specific key"""
        try:
            if self.redis is None:
                return False
            
            result = await self.redis.delete(key)
            return result > 0
        except redis.RedisError as e:
            logger.error(f"Redis error while deleting key {key}: {e}")
            return False
        except Exception as e:
            logger.error(f"Error deleting key {key}: {e}")
            return False

    async def delete_pattern(self, pattern: str) -> int:
        """Delete keys matching a pattern"""
        try:
            if self.redis is None:
                return 0
            
            # Get all keys matching the pattern
            keys = await self.redis.keys(pattern)
            if not keys:
                return 0
            
            # Delete all matching keys
            deleted_count = await self.redis.delete(*keys)
            return deleted_count
        except redis.RedisError as e:
            logger.error(f"Redis error while deleting pattern {pattern}: {e}")
            return 0
        except Exception as e:
            logger.error(f"Error deleting pattern {pattern}: {e}")
            return 0

    async def close(self):
        """Close Redis connection"""
        try:
            if self.redis:
                await self.redis.close()
                logger.info("Redis connection closed")
        except Exception as e:
            logger.error(f"Error closing Redis connection: {e}")
