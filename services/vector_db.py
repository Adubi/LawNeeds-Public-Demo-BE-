from pinecone import ServerlessSpec
from pinecone.grpc import PineconeGRPC as Pinecone
import os
import logging
import traceback
import math
logger = logging.getLogger(__name__)
DEFAULT_PINECONE_NAMESPACE = "default"

class VectorDB:
    def __init__(self):
        try:
            self.pinecone = Pinecone(api_key=os.getenv('PINECONE_API_KEY'))
            # Hardcode the index name instead of using environment variable
            self.index_name = "lawneeds-official-db"
            
            # Check if index exists
            existing_indexes = self.pinecone.list_indexes()
            
            if self.index_name not in existing_indexes:
                logger.info(f"Creating new Pinecone index: {self.index_name}")
                try:
                    self.pinecone.create_index(
                        name=self.index_name,
                        dimension=1536,  # OpenAI's text-embedding-ada-002 dimension
                        metric='cosine',
                        spec=ServerlessSpec(
                            cloud='aws',
                            region='us-east-1'
                        )
                    )
                except Exception as e:
                    if "ALREADY_EXISTS" in str(e):
                        logger.info(f"Index {self.index_name} already exists")
                    else:
                        raise e
            
            # Connect to the index
            self.index = self.pinecone.Index(self.index_name)
            logger.info(f"Successfully connected to index: {self.index_name}")
            
        except Exception as e:
            logger.error(f"Error initializing Pinecone: {str(e)}")
            raise

    @staticmethod
    def _sanitize_metadata_for_pinecone(metadata):
        """Return metadata using only Pinecone-supported value types."""
        sanitized = {}
        for key, value in (metadata or {}).items():
            if value is None:
                continue
            if isinstance(value, bool):
                sanitized[key] = value
            elif isinstance(value, str):
                sanitized[key] = value
            elif isinstance(value, (int, float)):
                if isinstance(value, float) and not math.isfinite(value):
                    continue
                sanitized[key] = value
            elif isinstance(value, list):
                string_values = [item for item in value if isinstance(item, str)]
                if string_values:
                    sanitized[key] = string_values
            else:
                sanitized[key] = str(value)
        return sanitized

    def store_embedding(self, embedding, metadata, namespace=DEFAULT_PINECONE_NAMESPACE):
        try:
            logger.info(f"Attempting to store embedding with metadata: {metadata}")
            
            # Ensure metadata has required fields
            if 'id' not in metadata:
                logger.error("Missing 'id' in metadata")
                raise ValueError("Metadata must contain 'id' field")
            
            # Convert embedding to list if needed
            embedding_list = embedding.tolist() if hasattr(embedding, 'tolist') else list(embedding)
            
            vector_id = str(metadata['id'])
            vector_data = {
                "id": vector_id,
                "values": embedding_list,
                "metadata": self._sanitize_metadata_for_pinecone(metadata)
            }
            
            logger.info(f"Upserting vector with ID: {vector_id}")
            
            # Upsert to Pinecone
            self.index.upsert(
                vectors=[vector_data],
                namespace=namespace or DEFAULT_PINECONE_NAMESPACE
            )
            
            logger.info(f"Successfully stored vector {vector_id}")
            return True
            
        except Exception as e:
            logger.error(f"Error storing embedding: {str(e)}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            return False

    def search_embeddings(self, query_embedding, top_k=5, namespace=DEFAULT_PINECONE_NAMESPACE):
        try:
            results = self.index.query(
                vector=query_embedding,
                top_k=top_k,
                include_metadata=True,
                namespace=namespace or DEFAULT_PINECONE_NAMESPACE
            )
            return results
        except Exception as e:
            logger.error(f"Error searching embeddings: {str(e)}")
            raise

    def delete_embeddings(self, vector_ids, namespace=DEFAULT_PINECONE_NAMESPACE):
        """Delete vectors by ID from a Pinecone namespace."""
        try:
            ids = [str(vector_id) for vector_id in vector_ids if vector_id]
            if not ids:
                return True

            target_namespace = namespace or DEFAULT_PINECONE_NAMESPACE
            self.index.delete(ids=ids, namespace=target_namespace)
            logger.info(f"Deleted {len(ids)} vectors from namespace {target_namespace}")
            return True
        except Exception as e:
            logger.error(f"Error deleting embeddings: {str(e)}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            return False

    def batch_upsert_embeddings(
        self,
        embeddings_with_metadata,
        batch_size=50,
        timeout=60,
        namespace=DEFAULT_PINECONE_NAMESPACE,
    ):
        """
        Batch upsert embeddings to Pinecone for better performance using async requests.
        
        Args:
            embeddings_with_metadata: List of tuples (embedding, metadata)
            batch_size: Size of batches for requests
            timeout: Timeout in seconds for the Pinecone requests
            
        Returns:
            bool: Whether the operation was successful
        """
        try:
            total_vectors = len(embeddings_with_metadata)
            target_namespace = namespace or DEFAULT_PINECONE_NAMESPACE
            logger.info(
                f"Batch upserting {total_vectors} vectors with batch size {batch_size} "
                f"to namespace {target_namespace}"
            )
            
            # Format all vectors first
            all_vectors = []
            for embedding, metadata in embeddings_with_metadata:
                # Convert embedding to list if needed
                embedding_list = embedding.tolist() if hasattr(embedding, 'tolist') else list(embedding)
                
                vector_id = str(metadata['id'])
                all_vectors.append({
                    "id": vector_id,
                    "values": embedding_list,
                    "metadata": self._sanitize_metadata_for_pinecone(metadata)
                })
            
            # Use a simple direct approach with timeout instead of async
            self.index.upsert(
                vectors=all_vectors,
                namespace=target_namespace,
                timeout=timeout  # Explicitly set timeout
            )
            
            logger.info(f"Successfully upserted {total_vectors} vectors")
            return True
            
        except Exception as e:
            logger.error(f"Error in batch upserting: {str(e)}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            return False
