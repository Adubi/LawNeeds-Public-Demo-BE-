import boto3
import os
from typing import Optional, Dict, Any
import uuid
from datetime import datetime
import logging
import re
from urllib.parse import urlsplit, unquote
from botocore.exceptions import ClientError, NoCredentialsError

logger = logging.getLogger(__name__)

class S3Service:
    """Service for handling S3 file uploads"""
    
    def __init__(self):
        """Initialize S3 client with environment variables"""
        self.bucket_name = os.getenv('S3_BUCKET_NAME')
        self.region = os.getenv('AWS_REGION', 'eu-west-2')
        
        try:
            self.s3_client = boto3.client(
                's3',
                aws_access_key_id=os.getenv('AWS_ACCESS_KEY_ID'),
                aws_secret_access_key=os.getenv('AWS_SECRET_ACCESS_KEY'),
                region_name=self.region
            )
            # Test connection and get actual bucket region
            response = self.s3_client.head_bucket(Bucket=self.bucket_name)
            
            # Try to get the actual bucket region
            try:
                bucket_location = self.s3_client.get_bucket_location(Bucket=self.bucket_name)
                actual_region = bucket_location['LocationConstraint']
                if actual_region is None:  # us-west-2 returns None
                    actual_region = 'eu-west-2'
                self.region = actual_region
                logger.info(f"Updated region to actual bucket region: {self.region}")
            except Exception as region_e:
                logger.warning(f"Could not determine bucket region: {region_e}")
            
            logger.info(f"S3 service initialized successfully for bucket: {self.bucket_name} in region: {self.region}")
        except (ClientError, NoCredentialsError) as e:
            logger.warning(f"S3 service initialization failed: {e}")
            self.s3_client = None
    
    def upload_file(self, file_content: bytes, file_name: str, content_type: str,
                   referral_id: str, file_type: str = "attachment") -> Optional[Dict[str, Any]]:
        """
        Upload file to S3 and return metadata

        Args:
            file_content: File content as bytes
            file_name: Original filename
            content_type: MIME type
            referral_id: Referral request ID for organization
            file_type: Type of file (voice_note, pdf, docx, image, attachment)

        Returns:
            Dict with file URL and metadata or None if upload fails
        """
        if not self.s3_client:
            logger.error("S3 client not available")
            return None
        
        try:
            # Generate unique key
            file_extension = file_name.split('.')[-1] if '.' in file_name else ''
            unique_filename = f"{uuid.uuid4()}.{file_extension}" if file_extension else str(uuid.uuid4())
            s3_key = f"referrals/{referral_id}/{file_type}/{unique_filename}"
            
            # Upload to S3
            self.s3_client.put_object(
                Bucket=self.bucket_name,
                Key=s3_key,
                Body=file_content,
                ContentType=content_type,
                Metadata={
                    'original_filename': file_name,
                    'referral_id': referral_id,
                    'file_type': file_type,
                    'upload_timestamp': datetime.utcnow().isoformat()
                }
            )
            
            # Generate presigned URL for better accessibility (24 hours expiration)
            presigned_url = self.generate_presigned_url(s3_key, expiration=86400)
            if not presigned_url:
                # Fallback to direct URL if presigned URL generation fails
                presigned_url = f"https://{self.bucket_name}.s3.{self.region}.amazonaws.com/{s3_key}"
            
            # Return metadata
            return {
                'url': presigned_url,
                's3_key': s3_key,
                'original_filename': file_name,
                'content_type': content_type,
                'file_size': len(file_content),
                'file_type': file_type,
                'upload_timestamp': datetime.utcnow().isoformat()
            }
            
        except Exception as e:
            logger.error(f"Error uploading file to S3: {e}")
            return None
    
    def delete_file(self, s3_key: str) -> bool:
        """
        Delete file from S3
        
        Args:
            s3_key: S3 object key
        
        Returns:
            True if successful, False otherwise
        """
        if not self.s3_client:
            logger.error("S3 client not available")
            return False
        
        try:
            self.s3_client.delete_object(Bucket=self.bucket_name, Key=s3_key)
            logger.info(f"Successfully deleted file: {s3_key}")
            return True
        except Exception as e:
            logger.error(f"Error deleting file from S3: {e}")
            return False
    
    def generate_presigned_url(self, s3_key: str, expiration: int = 3600,
                               source_url: Optional[str] = None) -> Optional[str]:
        """
        Generate presigned URL for file access
        
        Args:
            s3_key: S3 object key
            expiration: URL expiration time in seconds (default 1 hour)
        
        Returns:
            Presigned URL or None if generation fails
        """
        if not self.s3_client:
            logger.error("S3 client not available")
            return None
        
        try:
            bucket = self.bucket_name
            if source_url:
                location = self._s3_location(source_url)
                if not location:
                    logger.warning("Cannot refresh an unrecognized S3 source URL")
                    return None
                bucket, _ = location
                # Legacy uploads live in lawneeds; never sign arbitrary URL buckets.
                if bucket not in {self.bucket_name, "lawneeds"}:
                    logger.warning("Cannot refresh an unapproved S3 source bucket")
                    return None
            response = self.s3_client.generate_presigned_url(
                'get_object',
                Params={'Bucket': bucket, 'Key': s3_key},
                ExpiresIn=expiration
            )
            return response
        except Exception as e:
            logger.error(f"Error generating presigned URL: {e}")
            return None

    @staticmethod
    def _s3_location(url: str):
        """Parse AWS virtual-hosted/path-style URLs, decoding the object key once."""
        try:
            parsed = urlsplit(url)
            if parsed.scheme not in ("https", "http") or parsed.username or parsed.password:
                return None
            host = parsed.hostname or ""
            virtual = re.fullmatch(r"(.+)\.s3(?:[.-][a-z0-9-]+)?\.amazonaws\.com", host)
            path = parsed.path.lstrip("/")
            if virtual:
                return (virtual.group(1), unquote(path)) if path else None
            if re.fullmatch(r"s3(?:[.-][a-z0-9-]+)?\.amazonaws\.com", host):
                bucket, separator, key = path.partition("/")
                return (bucket, unquote(key)) if bucket and separator and key else None
        except ValueError:
            pass
        return None
    
    def extract_s3_key_from_url(self, url: str) -> Optional[str]:
        """
        Extract S3 key from a presigned URL or direct S3 URL.
        Used for backward compatibility when s3_key wasn't stored separately.
        
        Args:
            url: S3 presigned URL or direct URL
        
        Returns:
            S3 key or None if extraction fails
        """
        if not url:
            return None
        
        location = self._s3_location(url)
        return location[1] if location else None
    
    def refresh_presigned_url(self, url: str = None, s3_key: str = None, expiration: int = 3600) -> Optional[str]:
        """
        Generate a fresh presigned URL from either an existing URL or s3_key.
        
        Args:
            url: Existing S3 URL (presigned or direct) - used if s3_key not provided
            s3_key: S3 object key - preferred if available
            expiration: URL expiration time in seconds (default 1 hour)
        
        Returns:
            Fresh presigned URL or original URL if refresh fails
        """
        # Prefer s3_key if provided
        key = s3_key
        if not key and url:
            key = self.extract_s3_key_from_url(url)
        
        if not key:
            logger.warning("Could not determine S3 key for URL refresh")
            return url  # Return original URL as fallback
        
        fresh_url = self.generate_presigned_url(key, expiration, source_url=url)
        if fresh_url:
            return fresh_url
        
        # Fallback to original URL if generation fails
        return url
    
    def validate_file_type(self, content_type: str, file_type: str) -> bool:
        """
        Validate file type based on content type

        Args:
            content_type: MIME type
            file_type: Expected file type (voice_note, pdf, docx, image, document, attachment)

        Returns:
            True if valid, False otherwise
        """
        valid_types = {
            'voice_note': [
                'audio/mpeg', 'audio/mp3', 'audio/wav', 'audio/ogg',
                'audio/webm', 'audio/m4a', 'audio/aac', 'audio/x-wav'
            ],
            'pdf': ['application/pdf'],
            'docx': [
                'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                'application/msword'  # for .doc files
            ],
            'image': [
                'image/jpeg', 'image/jpg', 'image/png', 'image/gif',
                'image/bmp', 'image/webp', 'image/tiff', 'image/svg+xml'
            ],
            'document': [
                'application/pdf',
                'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                'application/msword'
            ],
            'attachment': [
                'application/pdf',
                'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                'application/msword',
                'audio/mpeg', 'audio/mp3', 'audio/wav', 'audio/ogg',
                'audio/webm', 'audio/m4a', 'audio/aac', 'audio/x-wav',
                'image/jpeg', 'image/jpg', 'image/png', 'image/gif',
                'image/bmp', 'image/webp', 'image/tiff', 'image/svg+xml'
            ],
            'chatbot_image': [
                'image/jpeg', 'image/jpg', 'image/png', 'image/gif',
                'image/bmp', 'image/webp'
            ],
            'chatbot_document': [
                'application/pdf'
            ]
        }

        return content_type in valid_types.get(file_type, [])
    
    def get_max_file_size(self, file_type: str) -> int:
        """
        Get maximum file size for file type in bytes

        Args:
            file_type: Type of file

        Returns:
            Maximum file size in bytes
        """
        max_sizes = {
            'voice_note': 50 * 1024 * 1024,  # 50MB
            'pdf': 25 * 1024 * 1024,         # 25MB
            'docx': 25 * 1024 * 1024,        # 25MB
            'image': 10 * 1024 * 1024,       # 10MB
            'document': 25 * 1024 * 1024,    # 25MB for documents
            'attachment': 50 * 1024 * 1024,  # 50MB
            'chatbot_image': 10 * 1024 * 1024,  # 10MB
            'chatbot_document': 25 * 1024 * 1024  # 25MB
        }

        return max_sizes.get(file_type, 10 * 1024 * 1024)  # Default 10MB
