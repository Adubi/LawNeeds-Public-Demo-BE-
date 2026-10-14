from datetime import datetime, timezone
from fastapi import HTTPException, APIRouter, BackgroundTasks
from fastapi.responses import JSONResponse
import aioboto3
import json
import os
import pymupdf
import hashlib
import re
from services.openai_service import generate_embedding
from services.vector_db import VectorDB
from services.rag_backfill_service import RAGBackfillConfig, RAGBackfillService
from services.structured_legal_parser import StructuredLegalDocumentParser
from services.legal_document_chunker import LegalDocumentChunker, LEGAL_CHUNKING_VERSION
import logging
from sqlalchemy import text
import uuid
from pathlib import Path
from io import BytesIO
from langchain.text_splitter import RecursiveCharacterTextSplitter
from typing import Optional, Dict, List, Any, Tuple
import traceback
from services.models import *
from services.db import *
from itertools import islice
from services.web_scrapper import WebScraper
from sqlalchemy.exc import IntegrityError
from services.models import ScrapedUrl, Base
from enum import Enum
import fitz  # PyMuPDF
from striprtf.striprtf import rtf_to_text
import io
import asyncio

logger = logging.getLogger(__name__)

DEFAULT_ALLOWED_KNOWLEDGEBASE_PREFIXES = (
    "Immigration Law/",
    "Family Law/",
    "Employment Law/",
    "Road Traffic Offences/",
)
SUPPORTED_KNOWLEDGEBASE_EXTENSIONS = (".pdf", ".rtf", ".docx")
KNOWLEDGEBASE_PARSER_VERSION = StructuredLegalDocumentParser.parser_version
KNOWLEDGEBASE_CHUNKING_VERSION = LEGAL_CHUNKING_VERSION
KNOWLEDGEBASE_EMBEDDING_NAMESPACE = os.getenv("KNOWLEDGEBASE_EMBEDDING_NAMESPACE", "default")
KNOWLEDGEBASE_EMBEDDING_MODEL = os.getenv("KNOWLEDGEBASE_EMBEDDING_MODEL", "text-embedding-3-small")
KNOWLEDGEBASE_PREFIX_REGISTRY_PATH = os.getenv(
    "KNOWLEDGEBASE_PREFIX_REGISTRY_PATH",
    str(Path(__file__).resolve().parent.parent / "config" / "knowledgebase_prefix_registry.json"),
)

# Define status types
class ProcessingStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"

class KnowledgeBasesAPI:
    def __init__(self, *, app_name, db, auth):
        self.app_name = app_name
        self.db = db
        self.auth = auth
        
        # Create aioboto3 session instead of direct client
        self.s3_session = aioboto3.Session(
            aws_access_key_id=os.getenv('AWS_ACCESS_KEY_ID'),
            aws_secret_access_key=os.getenv('AWS_SECRET_ACCESS_KEY')
        )
        
        # Initialize Vector DB
        self.vector_db = VectorDB()
        # Initialize text splitter
        self.text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=1000,
            chunk_overlap=100,
            length_function=len,
            separators=["\n\n", "\n", ".", " ", ""]
        )
        self.web_scraper = WebScraper()
        self.structured_parser = StructuredLegalDocumentParser()
        self.legal_chunker = LegalDocumentChunker()
        # Add a status tracker (in-memory for simplicity)
        self.processing_jobs = {}

    @staticmethod
    def _normalize_s3_prefix(prefix: str) -> str:
        prefix = (prefix or "").strip().lstrip("/")
        if prefix and not prefix.endswith("/"):
            prefix = f"{prefix}/"
        return prefix

    @classmethod
    def _load_knowledgebase_prefix_registry(cls) -> List[Dict[str, Any]]:
        registry_path = Path(KNOWLEDGEBASE_PREFIX_REGISTRY_PATH)
        try:
            raw_entries = json.loads(registry_path.read_text())
        except FileNotFoundError:
            logger.warning(f"Knowledge-base prefix registry not found: {registry_path}")
            raw_entries = []
        except json.JSONDecodeError as exc:
            logger.error(f"Invalid knowledge-base prefix registry JSON: {exc}")
            raw_entries = []

        entries = []
        for raw_entry in raw_entries:
            if not isinstance(raw_entry, dict) or not raw_entry.get("prefix"):
                continue
            entry = dict(raw_entry)
            entry["prefix"] = cls._normalize_s3_prefix(entry["prefix"])
            entries.append(entry)

        return entries

    def _get_registry_scan_prefixes(self) -> List[str]:
        registry_entries = self._load_knowledgebase_prefix_registry()
        scan_prefixes = [
            entry["prefix"]
            for entry in registry_entries
            if entry.get("scan_prefix") is True
            or ("document_type" not in entry and "source_authority" not in entry)
        ]
        if not scan_prefixes:
            scan_prefixes = [entry["prefix"] for entry in registry_entries]

        seen = set()
        deduped = []
        for prefix in scan_prefixes:
            if prefix not in seen:
                seen.add(prefix)
                deduped.append(prefix)
        return deduped

    @classmethod
    def _metadata_from_prefix_registry(cls, key: str) -> Dict[str, Any]:
        matches = [
            entry
            for entry in cls._load_knowledgebase_prefix_registry()
            if key.startswith(entry["prefix"])
        ]
        if not matches:
            return {}

        best_match = max(matches, key=lambda entry: len(entry["prefix"]))
        return {
            key: value
            for key, value in best_match.items()
            if key not in {"prefix", "scan_prefix"} and value is not None
        }

    def _get_allowed_knowledgebase_prefixes(self) -> List[str]:
        configured = (
            os.getenv("KNOWLEDGEBASE_ALLOWED_S3_PREFIXES")
            or os.getenv("KB_ALLOWED_S3_PREFIXES")
            or ""
        )
        if configured:
            prefixes = [
                "" if prefix.strip() in {"*", "/"} else self._normalize_s3_prefix(prefix)
                for prefix in configured.split(",")
                if prefix.strip()
            ]
        else:
            prefixes = self._get_registry_scan_prefixes() or list(DEFAULT_ALLOWED_KNOWLEDGEBASE_PREFIXES)

        # Remove duplicates while preserving order.
        seen = set()
        allowed_prefixes = []
        for prefix in prefixes:
            if prefix not in seen:
                seen.add(prefix)
                allowed_prefixes.append(prefix)

        return allowed_prefixes or list(DEFAULT_ALLOWED_KNOWLEDGEBASE_PREFIXES)

    def _resolve_allowed_directory_prefixes(self, directory_prefix: str) -> List[str]:
        requested_prefix = self._normalize_s3_prefix(directory_prefix)
        allowed_prefixes = self._get_allowed_knowledgebase_prefixes()

        if not requested_prefix:
            return allowed_prefixes

        if any(
            allowed_prefix == "" or requested_prefix.startswith(allowed_prefix)
            for allowed_prefix in allowed_prefixes
        ):
            return [requested_prefix]

        raise HTTPException(
            status_code=400,
            detail=(
                "Directory prefix is not allowed for knowledge-base ingestion. "
                f"Allowed prefixes: {', '.join(allowed_prefixes)}"
            ),
        )

    def _is_allowed_knowledgebase_key(self, key: str) -> bool:
        return any(
            prefix == "" or key.startswith(prefix)
            for prefix in self._get_allowed_knowledgebase_prefixes()
        )

    @staticmethod
    def _content_type_for_extension(file_extension: str) -> str:
        extension = file_extension.lower().lstrip(".")
        return {
            "pdf": "application/pdf",
            "rtf": "application/rtf",
            "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        }.get(extension, "application/octet-stream")

    @staticmethod
    def _infer_document_metadata_from_key(key: str) -> Dict[str, Any]:
        parts = [part for part in key.split("/") if part]
        top_level = parts[0] if parts else ""
        second_level = parts[1] if len(parts) > 1 else ""
        file_name = parts[-1] if parts else key
        title = os.path.splitext(file_name)[0].replace("_", " ").strip()
        registry_metadata = KnowledgeBasesAPI._metadata_from_prefix_registry(key)

        practice_area = registry_metadata.get("practice_area")
        if top_level in {
            "Immigration Law",
            "Family Law",
            "Employment Law",
            "Road Traffic Offences",
        } and not practice_area:
            practice_area = top_level

        document_type = registry_metadata.get("document_type")
        lower_second_level = second_level.lower()
        if not document_type and "case" in lower_second_level:
            document_type = "case"
        elif not document_type and "legislation" in lower_second_level:
            document_type = "legislation"
        elif not document_type and ("guidance" in lower_second_level or "policy" in lower_second_level):
            document_type = "guidance"
        elif not document_type and "explanatory" in lower_second_level:
            document_type = "explanatory_note"
        elif not document_type and "citizens advice" in lower_second_level:
            document_type = "citizens_advice"
        elif not document_type and "law commission" in lower_second_level:
            document_type = "law_commission_report"

        source_authority = registry_metadata.get("source_authority")
        if "acas" in key.lower():
            source_authority = "ACAS"
        elif "citizens advice" in key.lower():
            source_authority = "Citizens Advice"
        elif "cps" in key.lower():
            source_authority = "CPS"
        elif "law society" in key.lower():
            source_authority = "Law Society"
        elif not source_authority and practice_area:
            source_authority = practice_area

        return {
            "document_title": title or None,
            "document_type": document_type,
            "jurisdiction": registry_metadata.get("jurisdiction") or "UK",
            "practice_area": practice_area,
            "source_authority": source_authority,
            "published_at": KnowledgeBasesAPI._infer_publication_date_from_key(key),
        }

    @staticmethod
    def _infer_publication_date_from_key(key: str) -> Optional[datetime]:
        normalized = key.replace("_", " ").replace(".", " ").replace("-", " ")

        # YYYY MM DD
        match = re.search(r"\b(19|20)\d{2}\s+(0?[1-9]|1[0-2])\s+(0?[1-9]|[12]\d|3[01])\b", normalized)
        if match:
            parts = match.group(0).split()
            try:
                return datetime(int(parts[0]), int(parts[1]), int(parts[2]), tzinfo=timezone.utc)
            except ValueError:
                pass

        # DD MM YYYY
        match = re.search(r"\b(0?[1-9]|[12]\d|3[01])\s+(0?[1-9]|1[0-2])\s+((19|20)\d{2})\b", normalized)
        if match:
            parts = match.group(0).split()
            try:
                return datetime(int(parts[2]), int(parts[1]), int(parts[0]), tzinfo=timezone.utc)
            except ValueError:
                pass

        month_names = {
            "jan": 1,
            "january": 1,
            "feb": 2,
            "february": 2,
            "mar": 3,
            "march": 3,
            "apr": 4,
            "april": 4,
            "may": 5,
            "jun": 6,
            "june": 6,
            "jul": 7,
            "july": 7,
            "aug": 8,
            "august": 8,
            "sep": 9,
            "sept": 9,
            "september": 9,
            "oct": 10,
            "october": 10,
            "nov": 11,
            "november": 11,
            "dec": 12,
            "december": 12,
        }
        match = re.search(
            r"\b("
            + "|".join(month_names.keys())
            + r")\s+((19|20)\d{2})\b",
            normalized,
            flags=re.IGNORECASE,
        )
        if match:
            month = month_names[match.group(1).lower()]
            year = int(match.group(2))
            return datetime(year, month, 1, tzinfo=timezone.utc)

        return None

    @staticmethod
    def _datetimes_match(left: Optional[datetime], right: Optional[datetime]) -> bool:
        if not left or not right:
            return False
        if left == right:
            return True
        try:
            return left.isoformat() == right.isoformat()
        except AttributeError:
            return False

    def _processed_file_matches_current_pipeline(
        self,
        existing_file: Optional[KnowledgeBaseFile],
        *,
        s3_etag: Optional[str] = None,
        s3_version_id: Optional[str] = None,
        s3_last_modified: Optional[datetime] = None,
        content_hash: Optional[str] = None,
    ) -> Tuple[bool, str]:
        """Return whether a processed file can be safely skipped for the current RAG pipeline."""
        if not existing_file:
            return False, "not_previously_processed"

        pipeline_checks = [
            ("parser_version", existing_file.parser_version, KNOWLEDGEBASE_PARSER_VERSION),
            ("chunking_version", existing_file.chunking_version, KNOWLEDGEBASE_CHUNKING_VERSION),
            ("embedding_model", existing_file.embedding_model, KNOWLEDGEBASE_EMBEDDING_MODEL),
            ("embedding_namespace", existing_file.embedding_namespace, KNOWLEDGEBASE_EMBEDDING_NAMESPACE),
        ]
        for field_name, existing_value, current_value in pipeline_checks:
            if existing_value != current_value:
                return False, f"{field_name}_changed"

        if content_hash:
            if existing_file.content_hash == content_hash:
                return True, "content_hash_unchanged"
            return False, "content_hash_changed"

        if s3_version_id and existing_file.s3_version_id:
            if existing_file.s3_version_id == s3_version_id:
                return True, "s3_version_id_unchanged"
            return False, "s3_version_id_changed"

        if s3_etag and existing_file.s3_etag:
            if existing_file.s3_etag == s3_etag:
                return True, "s3_etag_unchanged"
            return False, "s3_etag_changed"

        if s3_last_modified and existing_file.s3_last_modified:
            if self._datetimes_match(existing_file.s3_last_modified, s3_last_modified):
                return True, "s3_last_modified_unchanged"
            return False, "s3_last_modified_changed_requires_content_hash"

        return False, "missing_reliable_change_token"

    @staticmethod
    def build_stable_vector_id(
        *,
        s3_key: str,
        content_hash: str,
        parser_version: str,
        chunking_version: str,
        embedding_model: str,
        chunk_index: int,
    ) -> str:
        """Build a deterministic vector ID for cross-environment ingestion."""
        fingerprint = "|".join(
            [
                s3_key,
                content_hash,
                parser_version,
                chunking_version,
                embedding_model,
                str(chunk_index),
            ]
        )
        digest = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()
        return f"kb:{digest}"

    def get_router(self):
        router = APIRouter()
        
        @router.post("/process_s3_pdfs")
        async def process_s3_pdfs(background_tasks: BackgroundTasks, directory_prefix: str = ""):
            try:
                # Hardcode the bucket name instead of using environment variable
                bucket_name = "lawneeds-official-db"
                allowed_directory_prefixes = self._resolve_allowed_directory_prefixes(directory_prefix)
                
                # Generate a unique job ID
                job_id = str(uuid.uuid4())
                
                # Initialize job status
                self.processing_jobs[job_id] = {
                    "status": ProcessingStatus.PENDING,
                    "bucket": bucket_name,
                    "directory": directory_prefix,
                    "directories": allowed_directory_prefixes,
                    "allowed_prefixes": self._get_allowed_knowledgebase_prefixes(),
                    "created_at": datetime.now()
                }
                
                # Start the processing in the background
                background_tasks.add_task(
                    self._process_s3_pdfs_background, 
                    bucket_name, 
                    job_id,
                    allowed_directory_prefixes
                )
                
                return JSONResponse(
                    status_code=202,
                    content={
                        "message": "PDF processing started in the background",
                        "job_id": job_id,
                        "status": ProcessingStatus.PENDING,
                        "directory": directory_prefix,
                        "directories": allowed_directory_prefixes
                    }
                )
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error in process_s3_pdfs: {str(e)}")
                raise HTTPException(status_code=500, detail=str(e))

        @router.post("/scrape_url")
        async def scrape_url(url: str):
            session = self.db.Session()
            try:
                # Create database entry
                url_entry = await self.create_url_entry(url)
                logger.info(f"Created URL entry with ID: {url_entry.id}")
                
                # Scrape content
                content = self.web_scraper.scrape_url(url)
                
                if 'error' in content:
                    await self.update_url_status(
                        url_entry.id, 
                        'failed', 
                        error_message=content['error']
                    )
                    raise HTTPException(
                        status_code=500, 
                        detail=f"Failed to scrape URL: {content['error']}"
                    )
                
                # Create chunks from the main content
                text_chunks = self.text_splitter.split_text(content['main_content'])
                logger.info(f"Split content into {len(text_chunks)} chunks")
                
                # Store in vector database
                processed_chunks = 0
                for i, chunk_text in enumerate(text_chunks):
                    try:
                        # Generate embedding for the chunk
                        embedding = generate_embedding(chunk_text)
                        
                        # Create metadata with required 'id' field
                        chunk_metadata = {
                            "id": f"{url_entry.id}_{i}",  # Unique ID for each chunk
                            "url_id": str(url_entry.id),
                            "source": url,
                            "title": content.get('title', ''),
                            "description": content.get('meta_description', ''),
                            "chunk_index": i,
                            "total_chunks": len(text_chunks),
                            "chunk_text": chunk_text[:100] + "..."  # Store preview of chunk
                        }
                        
                        logger.info(f"Storing chunk {i} with ID: {chunk_metadata['id']}")
                        
                        # Store in vector database
                        success = self.vector_db.store_embedding(embedding, chunk_metadata)
                        if success:
                            processed_chunks += 1
                            logger.info(f"Successfully stored chunk {i}")
                        else:
                            logger.error(f"Failed to store chunk {i}")
                            
                    except Exception as e:
                        logger.error(f"Error processing chunk {i}: {str(e)}")
                        continue
                
                # Update database entry
                await self.update_url_status(
                    url_entry.id,
                    'processed',
                    content=content['main_content'],
                    title=content.get('title', ''),
                    description=content.get('meta_description', ''),
                    chunk_count=processed_chunks,
                    vector_store_id=str(url_entry.id)
                )
                
                return JSONResponse(
                    status_code=200,
                    content={
                        "message": "Successfully scraped and processed URL",
                        "url_id": str(url_entry.id),
                        "title": content.get('title', ''),
                        "chunks_processed": processed_chunks,
                        "total_chunks": len(text_chunks)
                    }
                )
            
            except Exception as e:
                logger.error(f"Error processing URL {url}: {str(e)}")
                logger.error(f"Traceback: {traceback.format_exc()}")
                if 'url_entry' in locals():
                    await self.update_url_status(
                        url_entry.id,
                        'failed',
                        error_message=str(e)
                    )
                raise HTTPException(status_code=500, detail=str(e))
            finally:
                session.close()

        @router.get("/scraped_urls")
        async def get_scraped_urls():
            try:
                with self.db.get_session() as session:
                    urls = session.query(ScrapedUrl).order_by(ScrapedUrl.created_at.desc()).all()
                    return [{
                        "id": str(url.id),
                        "url": url.url,
                        "title": url.title,
                        "status": url.status,
                        "created_at": url.created_at.isoformat(),
                        "chunk_count": url.chunk_count
                    } for url in urls]
            except Exception as e:
                logger.error(f"Error fetching scraped URLs: {str(e)}")
                raise HTTPException(status_code=500, detail=str(e))
        
        @router.get("/processing_status/{job_id}")
        async def get_processing_status(job_id: str):
            if job_id not in self.processing_jobs:
                raise HTTPException(status_code=404, detail="Job not found")
            
            job_info = self.processing_jobs[job_id]
            
            return {
                "job_id": job_id,
                "status": job_info["status"],
                "bucket": job_info["bucket"],
                "directory": job_info.get("directory", ""),
                "created_at": job_info.get("created_at"),
                "start_time": job_info.get("start_time"),
                "end_time": job_info.get("end_time"),
                "total_files": job_info.get("total_files", 0),
                "processed_files": job_info.get("processed_files", 0),
                "failed_files": job_info.get("failed_files", 0),
                "completion_percentage": job_info.get("total_files", 0) > 0 and 
                    (job_info.get("processed_files", 0) + job_info.get("failed_files", 0)) / job_info.get("total_files", 0) * 100 or 0
            }

        @router.post("/admin/rag/backfill")
        async def start_rag_backfill(
            background_tasks: BackgroundTasks,
            namespace: str = "legal-aware-v1",
            bucket: str = "lawneeds-official-db",
            s3_prefix: str = "Immigration Law/LawNeeds DB UPDATE May 2026/",
            discovery_mode: str = "s3-query",
            vector_prefix: str = "",
            batch_size: int = 500,
            query_top_k: int = 10000,
            limit: int = 0,
            apply_changes: bool = True,
            replace_existing_chunks: bool = True,
        ):
            if discovery_mode not in {"pinecone-list", "s3-query"}:
                raise HTTPException(status_code=400, detail="discovery_mode must be pinecone-list or s3-query")
            if batch_size <= 0:
                raise HTTPException(status_code=400, detail="batch_size must be greater than 0")
            if query_top_k <= 0:
                raise HTTPException(status_code=400, detail="query_top_k must be greater than 0")
            if limit < 0:
                raise HTTPException(status_code=400, detail="limit cannot be negative")

            normalized_prefix = self._normalize_s3_prefix(s3_prefix)
            self._resolve_allowed_directory_prefixes(normalized_prefix)

            job_id = str(uuid.uuid4())
            config = RAGBackfillConfig(
                index=os.getenv("PINECONE_INDEX_NAME", "lawneeds-official-db"),
                namespace=namespace,
                bucket=bucket,
                s3_prefix=normalized_prefix,
                discovery_mode=discovery_mode,
                vector_prefix=vector_prefix,
                batch_size=batch_size,
                query_top_k=query_top_k,
                limit=limit,
                apply_changes=apply_changes,
                replace_existing_chunks=replace_existing_chunks,
            )
            now = datetime.now(timezone.utc)
            self.processing_jobs[job_id] = {
                "status": ProcessingStatus.PENDING,
                "type": "rag_backfill",
                "bucket": bucket,
                "namespace": namespace,
                "s3_prefix": normalized_prefix,
                "discovery_mode": discovery_mode,
                "apply_changes": apply_changes,
                "replace_existing_chunks": replace_existing_chunks,
                "created_at": now,
                "logs": [],
                "log_offset": 0,
                "config": config.__dict__.copy(),
                "inspected_vectors": 0,
                "usable_vectors": 0,
                "skipped_vectors": 0,
                "files_changed": 0,
                "chunks_changed": 0,
                "failed_files": 0,
            }
            self._append_rag_backfill_log(job_id, f"Queued RAG backfill job {job_id}")

            background_tasks.add_task(self._run_rag_backfill_background, job_id, config)

            return JSONResponse(
                status_code=202,
                content={
                    "message": "RAG backfill started in the background",
                    "job_id": job_id,
                    "status": ProcessingStatus.PENDING,
                    "status_url": f"/admin/rag/backfill/{job_id}",
                    "bucket": bucket,
                    "namespace": namespace,
                    "s3_prefix": normalized_prefix,
                    "discovery_mode": discovery_mode,
                    "apply_changes": apply_changes,
                    "replace_existing_chunks": replace_existing_chunks,
                },
            )

        @router.get("/admin/rag/backfill/{job_id}")
        async def get_rag_backfill_status(job_id: str, log_offset: int = 0):
            job_info = self.processing_jobs.get(job_id)
            if not job_info or job_info.get("type") != "rag_backfill":
                raise HTTPException(status_code=404, detail="RAG backfill job not found")

            stored_offset = int(job_info.get("log_offset", 0))
            logs = job_info.get("logs", [])
            requested_offset = max(log_offset, stored_offset)
            start_index = max(requested_offset - stored_offset, 0)
            selected_logs = logs[start_index:]
            next_offset = stored_offset + len(logs)

            return {
                "job_id": job_id,
                "status": job_info.get("status"),
                "bucket": job_info.get("bucket"),
                "namespace": job_info.get("namespace"),
                "s3_prefix": job_info.get("s3_prefix"),
                "discovery_mode": job_info.get("discovery_mode"),
                "apply_changes": job_info.get("apply_changes"),
                "replace_existing_chunks": job_info.get("replace_existing_chunks"),
                "created_at": job_info.get("created_at"),
                "start_time": job_info.get("start_time"),
                "end_time": job_info.get("end_time"),
                "duration_seconds": job_info.get("duration_seconds"),
                "mode": job_info.get("mode"),
                "error": job_info.get("error"),
                "config": job_info.get("config"),
                "progress": {
                    "inspected_vectors": job_info.get("inspected_vectors", 0),
                    "usable_vectors": job_info.get("usable_vectors", 0),
                    "skipped_vectors": job_info.get("skipped_vectors", 0),
                    "files_changed": job_info.get("files_changed", 0),
                    "chunks_changed": job_info.get("chunks_changed", 0),
                    "failed_files": job_info.get("failed_files", 0),
                    "s3_objects_found": job_info.get("s3_objects_found"),
                    "s3_objects_scanned": job_info.get("s3_objects_scanned", 0),
                },
                "log_offset": requested_offset,
                "next_log_offset": next_offset,
                "logs": selected_logs,
            }
        
        @router.post("/check_bucket_permissions")
        async def check_bucket_permissions():
            try:
                # Hardcode the bucket name instead of using environment variable
                bucket_name = "lawneeds-official-db"
                
                results = {
                    "bucket_name": bucket_name,
                    "tests": {}
                }
                
                # Test specific permissions one by one
                async with self.s3_session.client('s3') as s3_client:
                    # Test 1: Can we list buckets?
                    try:
                        response = await s3_client.list_buckets()
                        results["tests"]["list_buckets"] = "Success"
                    except Exception as e:
                        results["tests"]["list_buckets"] = f"Failed: {str(e)}"
                    
                    # Test 2: Can we check if the bucket exists?
                    try:
                        await s3_client.head_bucket(Bucket=bucket_name)
                        results["tests"]["head_bucket"] = "Success"
                    except Exception as e:
                        results["tests"]["head_bucket"] = f"Failed: {str(e)}"
                    
                    # Test 3: Can we list objects with a small limit?
                    try:
                        response = await s3_client.list_objects_v2(
                            Bucket=bucket_name,
                            MaxKeys=1
                        )
                        results["tests"]["list_objects"] = "Success"
                    except Exception as e:
                        results["tests"]["list_objects"] = f"Failed: {str(e)}"
                
                return results
            except Exception as e:
                logger.error(f"Bucket permission check failed: {str(e)}")
                return {
                    "status": "error",
                    "message": f"Bucket permission check failed: {str(e)}"
                }
        
        return router

    def _append_rag_backfill_log(self, job_id: str, line: str) -> None:
        job_info = self.processing_jobs.get(job_id)
        if not job_info:
            return

        logs = job_info.setdefault("logs", [])
        logs.append(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "line": line.rstrip(),
            }
        )

        max_logs = 1000
        if len(logs) > max_logs:
            prune_count = len(logs) - max_logs
            del logs[:prune_count]
            job_info["log_offset"] = int(job_info.get("log_offset", 0)) + prune_count

    def _update_rag_backfill_progress(self, job_id: str, progress) -> None:
        job_info = self.processing_jobs.get(job_id)
        if not job_info:
            return

        job_info["inspected_vectors"] = progress.inspected_vectors
        job_info["usable_vectors"] = progress.usable_vectors
        job_info["skipped_vectors"] = progress.skipped_vectors
        job_info["files_changed"] = progress.files_changed
        job_info["chunks_changed"] = progress.chunks_changed
        job_info["failed_files"] = progress.failed_files
        job_info["s3_objects_found"] = progress.s3_objects_found
        job_info["s3_objects_scanned"] = progress.s3_objects_scanned

    def _run_rag_backfill_background(self, job_id: str, config: RAGBackfillConfig) -> None:
        job_info = self.processing_jobs.get(job_id)
        if not job_info:
            return

        start_time = datetime.now(timezone.utc)
        job_info["status"] = ProcessingStatus.PROCESSING
        job_info["start_time"] = start_time
        self._append_rag_backfill_log(job_id, "Starting RAG backfill service")

        try:
            def progress_callback(message, progress):
                self._append_rag_backfill_log(job_id, message)
                self._update_rag_backfill_progress(job_id, progress)

            service = RAGBackfillService(
                db=self.db,
                logger_=logger,
                progress_callback=progress_callback,
            )
            result = service.run(config)
            self._update_rag_backfill_progress(job_id, result.progress)
            job_info["status"] = ProcessingStatus.COMPLETED
            job_info["mode"] = result.mode
            job_info["end_time"] = datetime.now(timezone.utc)
            job_info["duration_seconds"] = round((job_info["end_time"] - start_time).total_seconds(), 3)
            self._append_rag_backfill_log(job_id, "RAG backfill completed successfully")
        except Exception as e:
            logger.error(f"Error running RAG backfill job {job_id}: {str(e)}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            job_info["status"] = ProcessingStatus.FAILED
            job_info["error"] = str(e)
            job_info["end_time"] = datetime.now(timezone.utc)
            job_info["duration_seconds"] = round((job_info["end_time"] - start_time).total_seconds(), 3)
            self._append_rag_backfill_log(job_id, f"RAG backfill failed: {str(e)}")

    async def process_file(self, bucket_name: str, key: str) -> Optional[str]:
        """Process a file from S3 (PDF or RTF)."""
        try:
            if not self._is_allowed_knowledgebase_key(key):
                logger.warning(f"Skipping disallowed knowledge-base file: {key}")
                return None

            # Determine file type from extension
            file_extension = key.split('.')[-1].lower()

            try:
                async with self.s3_session.client('s3') as s3_client:
                    head_response = await s3_client.head_object(Bucket=bucket_name, Key=key)
                    s3_etag = (head_response.get("ETag") or "").strip('"') or None
                    s3_last_modified = head_response.get("LastModified")
                    s3_version_id = head_response.get("VersionId")
            except Exception as e:
                logger.error(f"Error reading S3 object metadata for {key}. Error: {str(e)}")
                return None

            # First, check whether the S3 object has already been processed
            # by the current parser/chunker/embedding pipeline.
            existing_file = None
            with self.db.get_session() as session:
                existing_file = session.query(KnowledgeBaseFile).filter_by(
                    bucket_name=bucket_name,
                    key=key,
                    status='processed'
                ).order_by(KnowledgeBaseFile.updated_at.desc()).first()

                should_skip, skip_reason = self._processed_file_matches_current_pipeline(
                    existing_file,
                    s3_etag=s3_etag,
                    s3_version_id=s3_version_id,
                    s3_last_modified=s3_last_modified,
                )
                if should_skip:
                    logger.info(
                        f"File {key} already processed for current RAG pipeline. "
                        f"Skipping. File ID: {existing_file.id}. Reason: {skip_reason}"
                    )
                    return str(existing_file.id)
                if existing_file:
                    logger.info(f"Re-embedding {key}. Reason: {skip_reason}")

            # If file not processed, continue with processing
            file_id = str(uuid.uuid4())
            current_time = datetime.now(timezone.utc)
            stale_vector_ids = self.get_existing_chunk_vector_ids(key)

            try:
                # Use session to create client
                async with self.s3_session.client('s3') as s3_client:
                    response = await s3_client.get_object(Bucket=bucket_name, Key=key)
                    file_content = await response['Body'].read()
                    file_obj = io.BytesIO(file_content)
            except Exception as e:
                logger.error(f"Error downloading file from S3: {key}. Error: {str(e)}")
                return None

            content_hash = hashlib.sha256(file_content).hexdigest()

            with self.db.get_session() as session:
                existing_file = session.query(KnowledgeBaseFile).filter_by(
                    bucket_name=bucket_name,
                    key=key,
                    status='processed'
                ).order_by(KnowledgeBaseFile.updated_at.desc()).first()

                should_skip, skip_reason = self._processed_file_matches_current_pipeline(
                    existing_file,
                    s3_etag=s3_etag,
                    s3_version_id=s3_version_id,
                    s3_last_modified=s3_last_modified,
                    content_hash=content_hash,
                )
                if should_skip:
                    logger.info(
                        f"File {key} content and pipeline are unchanged. "
                        f"Skipping. File ID: {existing_file.id}. Reason: {skip_reason}"
                    )
                    return str(existing_file.id)
                if existing_file:
                    logger.info(f"Continuing re-embedding for {key}. Reason: {skip_reason}")
                
            # Extract structured document content and render a text fallback
            # for validation/logging. Legal-aware chunks are produced from the
            # structured page/section/table output below.
            try:
                parsed_document = self.structured_parser.parse(
                    file_obj,
                    file_extension,
                    source_name=key,
                )
                text_content = self.structured_parser.to_plain_text(parsed_document)
            except Exception as e:
                logger.error(f"Failed to structurally parse {key}: {str(e)}")
                text_content = ""

            if not text_content:
                logger.error(f"Failed to extract text from {key}")
                return None

            parsed_page_count = len(parsed_document.get("pages", []))
            parsed_title = parsed_document.get("document_title")
            parsed_parser_version = parsed_document.get("parser_version") or KNOWLEDGEBASE_PARSER_VERSION
            inferred_metadata = self._infer_document_metadata_from_key(key)
                
            # Create database entry with 'pending' status
            content_type = self._content_type_for_extension(file_extension)
            self.create_file_entry(
                file_id=file_id,
                bucket_name=bucket_name,
                key=key,
                upload_date=current_time,
                content_type=content_type,
                file_size=len(file_content),
                s3_etag=s3_etag,
                s3_version_id=s3_version_id,
                s3_last_modified=s3_last_modified,
                content_hash=content_hash,
                document_title=parsed_title,
                parser_version=parsed_parser_version,
            )
            
            try:
                published_at = inferred_metadata.get("published_at")
                chunk_document_metadata = {
                    "file_id": file_id,
                    "s3_url": f"https://{bucket_name}.s3.amazonaws.com/{key}",
                    "s3_key": key,
                    "source": key,
                    "upload_date": current_time.isoformat(),
                    "document_title": parsed_title,
                    "document_type": inferred_metadata.get("document_type"),
                    "jurisdiction": inferred_metadata.get("jurisdiction"),
                    "practice_area": inferred_metadata.get("practice_area"),
                    "source_authority": inferred_metadata.get("source_authority"),
                    "published_at": published_at.isoformat() if published_at else "",
                    "is_current": True,
                    "s3_etag": s3_etag or "",
                    "content_hash": content_hash,
                    "parser_version": parsed_parser_version,
                    "chunking_version": KNOWLEDGEBASE_CHUNKING_VERSION,
                }

                chunks = self.legal_chunker.create_chunks(
                    parsed_document,
                    document_metadata=chunk_document_metadata,
                )

                # Prepare all embeddings and metadata in a batch
                embeddings_with_metadata = []
                
                # Process chunks and generate embeddings
                for i, chunk in enumerate(chunks):
                    try:
                        # Generate embedding
                        embedding = generate_embedding(chunk.page_content)
                        
                        vector_id = self.build_stable_vector_id(
                            s3_key=key,
                            content_hash=content_hash,
                            parser_version=parsed_parser_version,
                            chunking_version=chunk.metadata.get("chunking_version") or KNOWLEDGEBASE_CHUNKING_VERSION,
                            embedding_model=KNOWLEDGEBASE_EMBEDDING_MODEL,
                            chunk_index=i,
                        )

                        # Prepare metadata
                        metadata = {
                            "id": vector_id,
                            "file_id": file_id,
                            "chunk_id": str(i),
                            "text": chunk.page_content,
                            "s3_url": f"https://{bucket_name}.s3.amazonaws.com/{key}",
                            "s3_key": key,
                            "upload_date": current_time.isoformat(),
                            "document_title": chunk.metadata.get("document_title") or "",
                            "section_heading": chunk.metadata.get("section_heading") or "",
                            "page_number": chunk.metadata.get("page_number") or 0,
                            "section_index": chunk.metadata.get("section_index") or 0,
                            "section_chunk_index": chunk.metadata.get("section_chunk_index") or 0,
                            "previous_chunk_index": chunk.metadata.get("previous_chunk_index"),
                            "next_chunk_index": chunk.metadata.get("next_chunk_index"),
                            "contains_table": bool(chunk.metadata.get("contains_table")),
                            "table_count": chunk.metadata.get("table_count") or 0,
                            "document_type": chunk.metadata.get("document_type") or "",
                            "jurisdiction": chunk.metadata.get("jurisdiction") or "",
                            "practice_area": chunk.metadata.get("practice_area") or "",
                            "source_authority": chunk.metadata.get("source_authority") or "",
                            "published_at": chunk.metadata.get("published_at") or "",
                            "is_current": bool(chunk.metadata.get("is_current")),
                            "s3_etag": s3_etag or "",
                            "content_hash": content_hash,
                            "parser_version": parsed_parser_version,
                            "chunking_version": chunk.metadata.get("chunking_version") or KNOWLEDGEBASE_CHUNKING_VERSION,
                            "chunk_size": len(chunk.page_content)
                        }
                        
                        # Add to batch
                        embeddings_with_metadata.append((embedding, metadata))
                        logger.info(f"Generated embedding for chunk {i+1}/{len(chunks)} for file {key}")
                        
                    except Exception as e:
                        logger.error(f"Error generating embedding for chunk {i+1}/{len(chunks)} for file {key}: {str(e)}")
                        continue
                
                # Store embeddings in smaller batches to avoid timeouts
                processed_chunks = 0
                failed_chunks = 0
                successfully_upserted_metadata = []
                
                # Use a smaller batch size for vector database insertion to avoid timeouts
                max_vector_batch_size = 50  # Smaller batch size to avoid timeouts
                
                # Process in smaller batches
                if embeddings_with_metadata:
                    for i in range(0, len(embeddings_with_metadata), max_vector_batch_size):
                        batch = embeddings_with_metadata[i:i + max_vector_batch_size]
                        
                        # Try up to 3 times with exponential backoff
                        success = False
                        for attempt in range(3):
                            try:
                                success = self.vector_db.batch_upsert_embeddings(
                                    batch,
                                    namespace=KNOWLEDGEBASE_EMBEDDING_NAMESPACE,
                                )
                                if success:
                                    processed_chunks += len(batch)
                                    successfully_upserted_metadata.extend(metadata for _, metadata in batch)
                                    logger.info(f"Successfully processed vector batch {i//max_vector_batch_size + 1}/{(len(embeddings_with_metadata)-1)//max_vector_batch_size + 1} " 
                                               f"({len(batch)} chunks) for file {key}")
                                    break
                            except Exception as e:
                                logger.warning(f"Error in vector batch {i//max_vector_batch_size + 1} (attempt {attempt+1}/3): {str(e)}")
                                await asyncio.sleep(2 ** attempt)  # Exponential backoff: 1s, 2s, 4s
                        
                        if not success:
                            failed_chunks += len(batch)
                            logger.error(f"Failed to process vector batch {i//max_vector_batch_size + 1} after 3 attempts")
                            
                        # Add a small delay between batches to avoid overwhelming the service
                        await asyncio.sleep(1)
                else:
                    logger.warning(f"No valid embeddings generated for file {key}")

                deleted_stale_vectors = 0
                deleted_stale_chunk_records = 0
                superseded_file_records = 0
                if processed_chunks > 0:
                    if stale_vector_ids:
                        if self.vector_db.delete_embeddings(
                            stale_vector_ids,
                            namespace=KNOWLEDGEBASE_EMBEDDING_NAMESPACE,
                        ):
                            deleted_stale_vectors = len(stale_vector_ids)
                    deleted_stale_chunk_records = self.prune_existing_chunk_entries(key, exclude_file_id=file_id)

                persisted_chunk_records = self.create_chunk_entries(file_id, successfully_upserted_metadata)
                if persisted_chunk_records != processed_chunks:
                    logger.warning(
                        "Persisted chunk record count does not match processed vector count "
                        f"for {key}: persisted={persisted_chunk_records}, processed={processed_chunks}"
                    )

                if processed_chunks > 0:
                    superseded_file_records = self.mark_existing_file_versions_superseded(bucket_name, key, file_id)

                # Update processing statistics
                metadata = {
                    'file_size': len(file_content),
                    'page_count': parsed_page_count,
                    'chunk_count': len(chunks),
                    'processed_chunks': processed_chunks,
                    'failed_chunks': failed_chunks,
                    'persisted_chunk_records': persisted_chunk_records,
                    'deleted_stale_vectors': deleted_stale_vectors,
                    'deleted_stale_chunk_records': deleted_stale_chunk_records,
                    'superseded_file_records': superseded_file_records,
                }

                # Update status to processed and include processing metadata
                self.update_file_status(file_id, 'processed' if processed_chunks > 0 else 'error', metadata)
                
                logger.info(f"Successfully processed file {key}. "
                           f"Processed {processed_chunks}/{len(chunks)} chunks. "
                           f"Failed chunks: {failed_chunks}")
                
                return file_id

            except Exception as e:
                logger.error(f"Error during chunk processing for file {key}: {str(e)}")
                self.update_file_status(file_id, 'error')
                return None
        except Exception as e:
            logger.error(f"Unexpected error processing file {key}: {str(e)}")
            return None

    def batch(iterable, size):
        iterator = iter(iterable)
        return iter(lambda: list(islice(iterator, size)), [])

    def create_file_entry(
        self,
        file_id: str,
        bucket_name: str,
        key: str,
        upload_date: datetime,
        content_type: str,
        file_size: Optional[int] = None,
        s3_etag: Optional[str] = None,
        s3_version_id: Optional[str] = None,
        s3_last_modified: Optional[datetime] = None,
        content_hash: Optional[str] = None,
        document_title: Optional[str] = None,
        parser_version: Optional[str] = None,
    ) -> None:
        """Create a new file entry in the database."""
        file_name = key.split('/')[-1]
        s3_url = f"https://{bucket_name}.s3.amazonaws.com/{key}"
        inferred_metadata = self._infer_document_metadata_from_key(key)
        file_uuid = uuid.UUID(str(file_id)) if not isinstance(file_id, uuid.UUID) else file_id
        
        file_entry = KnowledgeBaseFile(
            id=file_uuid,
            file_name=file_name,
            s3_url=s3_url,
            bucket_name=bucket_name,
            key=key,
            s3_etag=s3_etag,
            s3_version_id=s3_version_id,
            s3_last_modified=s3_last_modified,
            content_hash=content_hash,
            status='pending',
            upload_date=upload_date,
            updated_at=upload_date,
            content_type=content_type,
            file_size=file_size,
            document_title=document_title or inferred_metadata.get("document_title"),
            source_url=s3_url,
            source_authority=inferred_metadata.get("source_authority"),
            document_type=inferred_metadata.get("document_type"),
            jurisdiction=inferred_metadata.get("jurisdiction"),
            practice_area=inferred_metadata.get("practice_area"),
            published_at=inferred_metadata.get("published_at"),
            is_current=True,
            parser_version=parser_version or KNOWLEDGEBASE_PARSER_VERSION,
            chunking_version=KNOWLEDGEBASE_CHUNKING_VERSION,
            embedding_model=KNOWLEDGEBASE_EMBEDDING_MODEL,
            embedding_namespace=KNOWLEDGEBASE_EMBEDDING_NAMESPACE
        )
        with self.db.get_session() as session:
            session.add(file_entry)
            session.commit()

    @staticmethod
    def _parse_iso_datetime(value) -> Optional[datetime]:
        if isinstance(value, datetime):
            return value
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value))
        except ValueError:
            return None

    def create_chunk_entries(self, file_id: str, chunk_metadata_items: List[Dict[str, Any]]) -> int:
        """Persist source-reference records for successfully embedded chunks."""
        if not chunk_metadata_items:
            return 0

        document_uuid = uuid.UUID(str(file_id)) if not isinstance(file_id, uuid.UUID) else file_id
        rows = []
        for item in chunk_metadata_items:
            vector_id = item.get("id")
            if not vector_id:
                continue
            rows.append(
                KnowledgeBaseChunk(
                    document_id=document_uuid,
                    vector_id=vector_id,
                    chunk_index=int(item.get("chunk_id") or item.get("chunk_index") or 0),
                    previous_chunk_index=item.get("previous_chunk_index"),
                    next_chunk_index=item.get("next_chunk_index"),
                    chunk_text=item.get("text") or "",
                    document_title=item.get("document_title") or None,
                    section_heading=item.get("section_heading") or None,
                    page_number=int(item.get("page_number") or 0) or None,
                    section_index=int(item.get("section_index") or 0),
                    section_chunk_index=int(item.get("section_chunk_index") or 0),
                    contains_table=bool(item.get("contains_table")),
                    table_count=int(item.get("table_count") or 0),
                    s3_url=item.get("s3_url") or None,
                    s3_key=item.get("s3_key") or item.get("source") or None,
                    source_url=item.get("s3_url") or None,
                    source_authority=item.get("source_authority") or None,
                    document_type=item.get("document_type") or None,
                    jurisdiction=item.get("jurisdiction") or None,
                    practice_area=item.get("practice_area") or None,
                    published_at=self._parse_iso_datetime(item.get("published_at")),
                    effective_from=self._parse_iso_datetime(item.get("effective_from")),
                    effective_to=self._parse_iso_datetime(item.get("effective_to")),
                    is_current=bool(item.get("is_current", True)),
                    content_hash=item.get("content_hash") or None,
                    parser_version=item.get("parser_version") or None,
                    chunking_version=item.get("chunking_version") or None,
                    embedding_model=KNOWLEDGEBASE_EMBEDDING_MODEL,
                    embedding_namespace=KNOWLEDGEBASE_EMBEDDING_NAMESPACE,
                )
            )

        if not rows:
            return 0

        with self.db.get_session() as session:
            for row in rows:
                existing = session.query(KnowledgeBaseChunk).filter_by(vector_id=row.vector_id).first()
                if existing:
                    session.delete(existing)
                    session.flush()
                session.add(row)
            session.commit()

        return len(rows)

    def get_existing_chunk_vector_ids(self, key: str, exclude_file_id: Optional[str] = None) -> List[str]:
        """Return existing vector IDs for a source key in the active embedding namespace."""
        exclude_uuid = uuid.UUID(str(exclude_file_id)) if exclude_file_id else None

        with self.db.get_session() as session:
            query = session.query(KnowledgeBaseChunk.vector_id).filter(
                KnowledgeBaseChunk.s3_key == key,
                KnowledgeBaseChunk.embedding_namespace == KNOWLEDGEBASE_EMBEDDING_NAMESPACE,
            )
            if exclude_uuid:
                query = query.filter(KnowledgeBaseChunk.document_id != exclude_uuid)
            return [row.vector_id for row in query.all() if row.vector_id]

    def prune_existing_chunk_entries(self, key: str, exclude_file_id: Optional[str] = None) -> int:
        """Delete stale chunk registry rows for a source key in the active namespace."""
        exclude_uuid = uuid.UUID(str(exclude_file_id)) if exclude_file_id else None
        with self.db.get_session() as session:
            query = session.query(KnowledgeBaseChunk).filter(
                KnowledgeBaseChunk.s3_key == key,
                KnowledgeBaseChunk.embedding_namespace == KNOWLEDGEBASE_EMBEDDING_NAMESPACE,
            )
            if exclude_uuid:
                query = query.filter(KnowledgeBaseChunk.document_id != exclude_uuid)

            deleted_count = query.delete(synchronize_session=False)
            session.commit()

        return deleted_count

    def mark_existing_file_versions_superseded(self, bucket_name: str, key: str, new_file_id: str) -> int:
        """Mark older processed records for the same source as superseded by the new file record."""
        new_uuid = uuid.UUID(str(new_file_id)) if not isinstance(new_file_id, uuid.UUID) else new_file_id
        updated_count = 0

        with self.db.get_session() as session:
            rows = session.query(KnowledgeBaseFile).filter(
                KnowledgeBaseFile.bucket_name == bucket_name,
                KnowledgeBaseFile.key == key,
                KnowledgeBaseFile.id != new_uuid,
                KnowledgeBaseFile.status == "processed",
            ).all()
            for row in rows:
                row.is_current = False
                row.superseded_by = str(new_uuid)
                updated_count += 1
            session.commit()

        return updated_count

    def update_file_status(self, file_id: str, status: str, metadata: dict = None) -> None:
        """Update the status and metadata of a file entry."""
        with self.db.get_session() as session:
            file_entry = session.query(KnowledgeBaseFile).filter_by(id=file_id).first()
            if file_entry:
                file_entry.status = status
                file_entry.updated_at = datetime.now(timezone.utc)
                
                if metadata:
                    file_entry.file_size = metadata.get('file_size')
                    file_entry.page_count = metadata.get('page_count')
                    file_entry.chunk_count = metadata.get('chunk_count')
                    
                session.commit()
                logger.info(f"Updated file {file_id} status to {status}")
            else:
                logger.error(f"File {file_id} not found in database")

    async def create_url_entry(self, url: str) -> ScrapedUrl:
        """Create a new URL entry in the database"""
        logger.info(f"Attempting to create URL entry for: {url}")
        session = self.db.Session()
        try:
            url_entry = ScrapedUrl(
                url=url,
                status='pending',
                embedding_model='text-embedding-3-small'
            )
            session.add(url_entry)
            session.commit()
            # Refresh the instance to ensure it's bound to the session
            session.refresh(url_entry)
            return url_entry
        except IntegrityError as ie:
            session.rollback()
            logger.error(f"IntegrityError creating URL entry: {str(ie)}")
            raise HTTPException(status_code=400, detail="URL already exists")
        except Exception as e:
            session.rollback()
            logger.error(f"Error creating URL entry: {str(e)}")
            raise HTTPException(status_code=500, detail=str(e))
        finally:
            session.close()

    async def update_url_status(
        self,
        url_id: uuid.UUID,
        status: str,
        error_message: Optional[str] = None,
        content: Optional[str] = None,
        title: Optional[str] = None,
        description: Optional[str] = None,
        chunk_count: Optional[int] = None,
        vector_store_id: Optional[str] = None
    ) -> None:
        """Update the status and metadata of a URL entry"""
        session = self.db.Session()
        try:
            url_entry = session.query(ScrapedUrl).filter_by(id=url_id).first()
            if url_entry:
                url_entry.status = status
                url_entry.updated_at = datetime.now(timezone.utc)
                
                if error_message:
                    url_entry.error_message = error_message
                if content:
                    url_entry.content = content
                if title:
                    url_entry.title = title
                if description:
                    url_entry.description = description
                if chunk_count:
                    url_entry.chunk_count = chunk_count
                if vector_store_id:
                    url_entry.vector_store_id = vector_store_id
                
                session.commit()
                logger.info(f"Updated URL {url_id} status to {status}")
            else:
                logger.error(f"URL {url_id} not found in database")
        except Exception as e:
            session.rollback()
            logger.error(f"Error updating URL {url_id} status: {str(e)}")
            raise
        finally:
            session.close()

    
    async def _process_s3_pdfs_background(self, bucket_name: str, job_id: str, directory_prefixes=None):
        try:
            if directory_prefixes is None:
                directory_prefixes = self._get_allowed_knowledgebase_prefixes()
            elif isinstance(directory_prefixes, str):
                directory_prefixes = self._resolve_allowed_directory_prefixes(directory_prefixes)
            else:
                directory_prefixes = [
                    self._normalize_s3_prefix(prefix)
                    for prefix in directory_prefixes
                ]

            allowed_prefixes = self._get_allowed_knowledgebase_prefixes()
            for prefix in directory_prefixes:
                if not any(
                    allowed_prefix == "" or prefix.startswith(allowed_prefix)
                    for allowed_prefix in allowed_prefixes
                ):
                    raise ValueError(f"Disallowed knowledge-base prefix: {prefix}")

            # Update status to processing
            self.processing_jobs[job_id] = {
                "status": ProcessingStatus.PROCESSING,
                "bucket": bucket_name,
                "directory": directory_prefixes[0] if len(directory_prefixes) == 1 else "",
                "directories": directory_prefixes,
                "allowed_prefixes": allowed_prefixes,
                "start_time": datetime.now(),
                "total_files": 0,
                "processed_files": 0,
                "failed_files": 0,
                "file_ids": []
            }
            
            # List PDFs and RTFs only inside approved legal-library prefixes.
            file_keys = set()
            
            # Use session to create resource
            async with self.s3_session.resource('s3') as s3:
                bucket = await s3.Bucket(bucket_name)
                for directory_prefix in directory_prefixes:
                    async for obj in bucket.objects.filter(Prefix=directory_prefix):
                        if (
                            obj.key.lower().endswith(SUPPORTED_KNOWLEDGEBASE_EXTENSIONS)
                            and self._is_allowed_knowledgebase_key(obj.key)
                        ):
                            file_keys.add(obj.key)

            files = sorted(file_keys)
            
            # Update total files count
            self.processing_jobs[job_id]["total_files"] = len(files)
            
            # Process files in batches
            batch_size = 5  # Process 5 files at a time
            for i in range(0, len(files), batch_size):
                batch = files[i:i+batch_size]
                batch_tasks = []
                
                # Create tasks for batch processing
                for key in batch:
                    task = asyncio.create_task(self.process_file(bucket_name, key))
                    batch_tasks.append((key, task))
                
                # Wait for all tasks in this batch to complete
                for key, task in batch_tasks:
                    try:
                        file_id = await task
                        if file_id:
                            self.processing_jobs[job_id]["file_ids"].append(str(file_id))
                            self.processing_jobs[job_id]["processed_files"] += 1
                    except Exception as e:
                        logger.error(f"Error processing file {key}: {str(e)}")
                        self.processing_jobs[job_id]["failed_files"] += 1
                
                # Add a small pause between batches
                if i + batch_size < len(files):
                    await asyncio.sleep(10)  # 10-second pause between batches
            
            # Update status to completed
            self.processing_jobs[job_id]["status"] = ProcessingStatus.COMPLETED
            self.processing_jobs[job_id]["end_time"] = datetime.now()
            
        except Exception as e:
            logger.error(f"Error in background processing: {str(e)}")
            self.processing_jobs[job_id]["status"] = ProcessingStatus.FAILED
            self.processing_jobs[job_id]["error"] = str(e)
            self.processing_jobs[job_id]["end_time"] = datetime.now()

    @staticmethod
    def extract_text_from_rtf(rtf_file):
        """Extract text from RTF file."""
        try:
            # Read the RTF content
            rtf_content = rtf_file.read().decode('utf-8', errors='ignore')
            
            # Convert RTF to plain text
            plain_text = rtf_to_text(rtf_content)
            
            return plain_text
        except Exception as e:
            logger.error(f"Error extracting text from RTF: {str(e)}")
            return ""

    @staticmethod
    def extract_text_from_file(file_obj, file_type):
        """Extract text based on file type."""
        if file_type.lower() == 'pdf':
            # Use PyMuPDF for PDF extraction
            try:
                # Create a copy of the file object to avoid consumption issues
                file_copy = io.BytesIO(file_obj.read())
                file_obj.seek(0)  # Reset the original file pointer
                
                doc = fitz.open(stream=file_copy.read(), filetype="pdf")
                text = ""
                for page_num in range(len(doc)):
                    page = doc.load_page(page_num)
                    text += page.get_text() + "\n"
                return text
            except Exception as e:
                logger.error(f"Error extracting text from PDF: {str(e)}")
                return ""
            
        elif file_type.lower() == 'rtf':
            # Use striprtf for RTF extraction
            return KnowledgeBasesAPI.extract_text_from_rtf(file_obj)
        elif file_type.lower() == 'docx':
            try:
                from docx import Document as DocxDocument
            except ImportError:
                logger.error("python-docx is required to extract text from DOCX files")
                return ""

            try:
                file_obj.seek(0)
                document = DocxDocument(file_obj)
                paragraphs = [paragraph.text for paragraph in document.paragraphs if paragraph.text.strip()]
                table_text = []
                for table in document.tables:
                    for row in table.rows:
                        cells = [cell.text.strip() for cell in row.cells]
                        if any(cells):
                            table_text.append(" | ".join(cells))
                return "\n".join(paragraphs + table_text)
            except Exception as e:
                logger.error(f"Error extracting text from DOCX: {str(e)}")
                return ""
        else:
            logger.error(f"Unsupported file type: {file_type}")
            return ""
