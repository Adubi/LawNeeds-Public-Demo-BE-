from __future__ import annotations

import logging
import os
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

import boto3
from pinecone.grpc import PineconeGRPC as Pinecone

from services.legal_document_chunker import LEGAL_CHUNKING_VERSION
from services.models import KnowledgeBaseChunk, KnowledgeBaseFile
from services.structured_legal_parser import StructuredLegalDocumentParser


logger = logging.getLogger(__name__)

KNOWLEDGEBASE_PARSER_VERSION = StructuredLegalDocumentParser.parser_version
KNOWLEDGEBASE_CHUNKING_VERSION = LEGAL_CHUNKING_VERSION
KNOWLEDGEBASE_EMBEDDING_NAMESPACE = os.getenv("KNOWLEDGEBASE_EMBEDDING_NAMESPACE", "default")
KNOWLEDGEBASE_EMBEDDING_MODEL = os.getenv("KNOWLEDGEBASE_EMBEDDING_MODEL", "text-embedding-3-small")


@dataclass
class RAGBackfillConfig:
    index: str = "lawneeds-official-db"
    namespace: str = KNOWLEDGEBASE_EMBEDDING_NAMESPACE
    bucket: str = "lawneeds-official-db"
    s3_prefix: str = "Immigration Law/"
    vector_prefix: str = "kb:"
    batch_size: int = 100
    limit: int = 0
    discovery_mode: str = "pinecone-list"
    query_top_k: int = 10000
    apply_changes: bool = False
    replace_existing_chunks: bool = False


@dataclass
class RAGBackfillProgress:
    inspected_vectors: int = 0
    usable_vectors: int = 0
    skipped_vectors: int = 0
    files_changed: int = 0
    chunks_changed: int = 0
    failed_files: int = 0
    s3_objects_found: Optional[int] = None
    s3_objects_scanned: int = 0


@dataclass
class RAGBackfillResult:
    mode: str
    config: RAGBackfillConfig
    progress: RAGBackfillProgress


ProgressCallback = Callable[[str, RAGBackfillProgress], None]


def parse_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def as_int(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def as_bool(value: Any, default: bool = True) -> bool:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    return bool(value)


def as_uuid(value: Any) -> Optional[uuid.UUID]:
    if not value:
        return None
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def sanitize_string(value: Any) -> Any:
    if isinstance(value, str):
        return value.replace("\x00", "")
    return value


def sanitize_metadata(metadata: Dict[str, Any]) -> Dict[str, Any]:
    sanitized = {}
    for key, value in (metadata or {}).items():
        if isinstance(value, str):
            sanitized[key] = value.replace("\x00", "")
        elif isinstance(value, list):
            sanitized[key] = [sanitize_string(item) for item in value]
        elif isinstance(value, dict):
            sanitized[key] = sanitize_metadata(value)
        else:
            sanitized[key] = value
    return sanitized


def chunks(items: List[str], size: int) -> Iterable[List[str]]:
    for index in range(0, len(items), size):
        yield items[index : index + size]


def pinecone_id_batches(index, namespace: str, vector_prefix: str) -> Iterable[List[str]]:
    listed = index.list(namespace=namespace, prefix=vector_prefix)
    for page in listed:
        if isinstance(page, dict):
            ids = page.get("vectors") or page.get("ids") or []
            yield [item.get("id") if isinstance(item, dict) else item for item in ids]
        else:
            yield [str(item) for item in page]


def fetch_vectors(index, namespace: str, vector_ids: List[str]) -> Dict[str, Any]:
    response = index.fetch(ids=vector_ids, namespace=namespace)
    if isinstance(response, dict):
        return response.get("vectors") or {}
    return dict(getattr(response, "vectors", {}) or {})


def vector_metadata(vector: Any) -> Dict[str, Any]:
    if isinstance(vector, dict):
        return sanitize_metadata(dict(vector.get("metadata") or {}))
    return sanitize_metadata(dict(getattr(vector, "metadata", {}) or {}))


def match_id(match: Any) -> Optional[str]:
    if isinstance(match, dict):
        return match.get("id")
    return getattr(match, "id", None)


def match_metadata(match: Any) -> Dict[str, Any]:
    if isinstance(match, dict):
        return dict(match.get("metadata") or {})
    return dict(getattr(match, "metadata", {}) or {})


def describe_index_dimension(index) -> int:
    stats = index.describe_index_stats()
    if isinstance(stats, dict):
        return as_int(stats.get("dimension")) or 1536
    return as_int(getattr(stats, "dimension", None)) or 1536


def list_s3_keys(bucket: str, s3_prefix: str) -> List[str]:
    s3 = boto3.client(
        "s3",
        aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
        region_name=os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION"),
    )
    paginator = s3.get_paginator("list_objects_v2")
    keys = []
    for page in paginator.paginate(Bucket=bucket, Prefix=s3_prefix):
        for item in page.get("Contents", []):
            key = item.get("Key")
            if key and not key.endswith("/"):
                keys.append(key)
    return keys


def query_vectors_for_s3_key(
    index,
    *,
    namespace: str,
    s3_key: str,
    top_k: int,
    dimension: int,
) -> Dict[str, Any]:
    probe = [0.0] * dimension
    probe[0] = 1.0
    response = index.query(
        vector=probe,
        top_k=top_k,
        namespace=namespace,
        filter={"s3_key": {"$eq": s3_key}},
        include_metadata=True,
    )
    matches = response.get("matches") if isinstance(response, dict) else getattr(response, "matches", [])
    vectors: Dict[str, Any] = {}
    for match in matches or []:
        vector_id = match_id(match)
        if vector_id:
            vectors[str(vector_id)] = {"metadata": match_metadata(match)}
    return vectors


def file_content_type(s3_key: str) -> str:
    extension = Path(s3_key).suffix.lower().lstrip(".")
    return {
        "pdf": "application/pdf",
        "rtf": "application/rtf",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }.get(extension, "application/octet-stream")


def upsert_file_row(
    session,
    *,
    bucket: str,
    s3_key: str,
    namespace: str,
    metadata: Dict[str, Any],
    dry_run: bool,
) -> uuid.UUID:
    metadata_file_id = as_uuid(metadata.get("file_id") or metadata.get("document_id"))
    existing = (
        session.query(KnowledgeBaseFile)
        .filter(
            KnowledgeBaseFile.bucket_name == bucket,
            KnowledgeBaseFile.key == s3_key,
            KnowledgeBaseFile.status == "processed",
        )
        .order_by(KnowledgeBaseFile.updated_at.desc())
        .first()
    )
    if not existing and metadata_file_id:
        existing = session.query(KnowledgeBaseFile).filter(KnowledgeBaseFile.id == metadata_file_id).first()
    if not existing:
        existing = (
            session.query(KnowledgeBaseFile)
            .filter(
                KnowledgeBaseFile.bucket_name == bucket,
                KnowledgeBaseFile.key == s3_key,
            )
            .order_by(KnowledgeBaseFile.updated_at.desc())
            .first()
        )

    file_id = existing.id if existing else metadata_file_id or uuid.uuid4()
    if dry_run:
        return file_id

    now = datetime.now(timezone.utc)
    s3_url = metadata.get("s3_url") or f"https://{bucket}.s3.amazonaws.com/{s3_key}"
    upload_date = parse_datetime(metadata.get("upload_date")) or now

    if not existing:
        existing = KnowledgeBaseFile(
            id=file_id,
            bucket_name=bucket,
            key=s3_key,
            file_name=Path(s3_key).name,
            s3_url=s3_url,
            upload_date=upload_date,
            content_type=file_content_type(s3_key),
        )
        session.add(existing)

    existing.status = "processed"
    existing.updated_at = now
    existing.s3_url = s3_url
    existing.source_url = metadata.get("source_url") or s3_url
    existing.s3_etag = metadata.get("s3_etag") or existing.s3_etag
    existing.content_hash = metadata.get("content_hash") or existing.content_hash
    existing.document_title = metadata.get("document_title") or existing.document_title or Path(s3_key).stem
    existing.source_authority = metadata.get("source_authority") or existing.source_authority
    existing.document_type = metadata.get("document_type") or existing.document_type
    existing.jurisdiction = metadata.get("jurisdiction") or existing.jurisdiction
    existing.practice_area = metadata.get("practice_area") or existing.practice_area
    existing.published_at = parse_datetime(metadata.get("published_at")) or existing.published_at
    existing.effective_from = parse_datetime(metadata.get("effective_from")) or existing.effective_from
    existing.effective_to = parse_datetime(metadata.get("effective_to")) or existing.effective_to
    existing.is_current = as_bool(metadata.get("is_current"), True)
    existing.parser_version = metadata.get("parser_version") or KNOWLEDGEBASE_PARSER_VERSION
    existing.chunking_version = metadata.get("chunking_version") or KNOWLEDGEBASE_CHUNKING_VERSION
    existing.embedding_model = metadata.get("embedding_model") or KNOWLEDGEBASE_EMBEDDING_MODEL
    existing.embedding_namespace = namespace

    return file_id


def chunk_row_from_metadata(
    *,
    document_id: uuid.UUID,
    vector_id: str,
    namespace: str,
    metadata: Dict[str, Any],
) -> KnowledgeBaseChunk:
    chunk_index = as_int(metadata.get("chunk_id")) or as_int(metadata.get("chunk_index")) or 0
    page_number = as_int(metadata.get("page_number"))
    return KnowledgeBaseChunk(
        document_id=document_id,
        vector_id=vector_id,
        chunk_index=chunk_index,
        previous_chunk_index=as_int(metadata.get("previous_chunk_index")),
        next_chunk_index=as_int(metadata.get("next_chunk_index")),
        chunk_text=metadata.get("text") or metadata.get("chunk_text") or "",
        document_title=metadata.get("document_title") or None,
        section_heading=metadata.get("section_heading") or None,
        page_number=page_number if page_number else None,
        section_index=as_int(metadata.get("section_index")) or 0,
        section_chunk_index=as_int(metadata.get("section_chunk_index")) or 0,
        contains_table=as_bool(metadata.get("contains_table"), False),
        table_count=as_int(metadata.get("table_count")) or 0,
        s3_url=metadata.get("s3_url") or None,
        s3_key=metadata.get("s3_key") or None,
        source_url=metadata.get("source_url") or metadata.get("s3_url") or None,
        source_authority=metadata.get("source_authority") or None,
        document_type=metadata.get("document_type") or None,
        jurisdiction=metadata.get("jurisdiction") or None,
        practice_area=metadata.get("practice_area") or None,
        published_at=parse_datetime(metadata.get("published_at")),
        effective_from=parse_datetime(metadata.get("effective_from")),
        effective_to=parse_datetime(metadata.get("effective_to")),
        is_current=as_bool(metadata.get("is_current"), True),
        content_hash=metadata.get("content_hash") or None,
        parser_version=metadata.get("parser_version") or KNOWLEDGEBASE_PARSER_VERSION,
        chunking_version=metadata.get("chunking_version") or KNOWLEDGEBASE_CHUNKING_VERSION,
        embedding_model=metadata.get("embedding_model") or KNOWLEDGEBASE_EMBEDDING_MODEL,
        embedding_namespace=namespace,
    )


class RAGBackfillService:
    def __init__(
        self,
        *,
        db,
        logger_: Optional[logging.Logger] = None,
        progress_callback: Optional[ProgressCallback] = None,
    ):
        self.db = db
        self.logger = logger_ or logger
        self.progress_callback = progress_callback

    def run(self, config: RAGBackfillConfig) -> RAGBackfillResult:
        if not os.getenv("PINECONE_API_KEY"):
            raise RuntimeError("PINECONE_API_KEY is required")
        if config.discovery_mode not in {"pinecone-list", "s3-query"}:
            raise ValueError("discovery_mode must be pinecone-list or s3-query")

        pinecone = Pinecone(api_key=os.getenv("PINECONE_API_KEY"))
        index = pinecone.Index(config.index)
        progress = RAGBackfillProgress()

        self._emit("Starting RAG backfill with config=%s" % asdict(config), progress)
        if config.discovery_mode == "s3-query":
            self._run_s3_query_mode(index, config, progress)
        else:
            self._run_pinecone_list_mode(index, config, progress)

        mode = "APPLIED" if config.apply_changes else "DRY RUN"
        self._emit(
            (
                "%s complete: inspected_vectors=%s usable_vectors=%s files=%s "
                "chunks=%s namespace=%s s3_prefix=%s"
            )
            % (
                mode,
                progress.inspected_vectors,
                progress.usable_vectors,
                progress.files_changed,
                progress.chunks_changed,
                config.namespace,
                config.s3_prefix,
            ),
            progress,
        )
        return RAGBackfillResult(mode=mode, config=config, progress=progress)

    def _run_s3_query_mode(self, index, config: RAGBackfillConfig, progress: RAGBackfillProgress) -> None:
        s3_keys = list_s3_keys(config.bucket, config.s3_prefix)
        if config.limit:
            s3_keys = s3_keys[: config.limit]
        dimension = describe_index_dimension(index)
        progress.s3_objects_found = len(s3_keys)
        self._emit(
            "S3 query discovery found %s objects under s3://%s/%s; dimension=%s"
            % (len(s3_keys), config.bucket, config.s3_prefix, dimension),
            progress,
        )

        for scanned_s3_objects, s3_key in enumerate(s3_keys, start=1):
            fetched = query_vectors_for_s3_key(
                index,
                namespace=config.namespace,
                s3_key=s3_key,
                top_k=config.query_top_k,
                dimension=dimension,
            )
            vector_items: List[tuple[str, Dict[str, Any]]] = []
            progress.inspected_vectors += len(fetched)
            progress.s3_objects_scanned = scanned_s3_objects
            for vector_id, vector in fetched.items():
                metadata = vector_metadata(vector)
                metadata.setdefault("s3_key", s3_key)
                if not metadata.get("text") and not metadata.get("chunk_text"):
                    progress.skipped_vectors += 1
                    continue
                vector_items.append((str(vector_id), metadata))
                progress.usable_vectors += 1

            if vector_items:
                self._write_file_vectors_safely(s3_key, vector_items, config, progress)

            self._emit(
                "Scanned %s vectors from %s S3 objects; usable=%s skipped=%s files=%s chunks=%s"
                % (
                    progress.inspected_vectors,
                    progress.s3_objects_scanned,
                    progress.usable_vectors,
                    progress.skipped_vectors,
                    progress.files_changed,
                    progress.chunks_changed,
                ),
                progress,
            )

    def _run_pinecone_list_mode(self, index, config: RAGBackfillConfig, progress: RAGBackfillProgress) -> None:
        grouped: Dict[str, List[tuple[str, Dict[str, Any]]]] = {}
        for id_page in pinecone_id_batches(index, config.namespace, config.vector_prefix):
            ids = [vector_id for vector_id in id_page if vector_id]
            if config.limit:
                remaining = max(config.limit - progress.inspected_vectors, 0)
                if remaining <= 0:
                    break
                ids = ids[:remaining]
            if not ids:
                continue

            for id_batch in chunks(ids, config.batch_size):
                fetched = fetch_vectors(index, config.namespace, id_batch)
                progress.inspected_vectors += len(id_batch)
                for vector_id, vector in fetched.items():
                    metadata = vector_metadata(vector)
                    s3_key = metadata.get("s3_key")
                    if not s3_key or not str(s3_key).startswith(config.s3_prefix):
                        progress.skipped_vectors += 1
                        continue
                    if not metadata.get("text") and not metadata.get("chunk_text"):
                        progress.skipped_vectors += 1
                        continue
                    grouped.setdefault(str(s3_key), []).append((str(vector_id), metadata))
                    progress.usable_vectors += 1

            self._emit(
                "Scanned %s vectors; usable=%s skipped=%s"
                % (progress.inspected_vectors, progress.usable_vectors, progress.skipped_vectors),
                progress,
            )

        for s3_key, vector_items in sorted(grouped.items()):
            self._write_file_vectors_safely(s3_key, vector_items, config, progress)

    def _write_file_vectors_safely(
        self,
        s3_key: str,
        vector_items: List[tuple[str, Dict[str, Any]]],
        config: RAGBackfillConfig,
        progress: RAGBackfillProgress,
    ) -> None:
        try:
            self._write_file_vectors(s3_key, vector_items, config, progress)
        except Exception as exc:
            progress.failed_files += 1
            self._emit(
                "Failed to write file s3_key=%s vector_count=%s error=%s"
                % (s3_key, len(vector_items), exc),
                progress,
            )

    def _write_file_vectors(
        self,
        s3_key: str,
        vector_items: List[tuple[str, Dict[str, Any]]],
        config: RAGBackfillConfig,
        progress: RAGBackfillProgress,
    ) -> None:
        if not vector_items:
            return

        with self.db.get_session() as session:
            representative_metadata = vector_items[0][1]
            document_id = upsert_file_row(
                session,
                bucket=config.bucket,
                s3_key=s3_key,
                namespace=config.namespace,
                metadata=representative_metadata,
                dry_run=not config.apply_changes,
            )
            progress.files_changed += 1

            if config.apply_changes and config.replace_existing_chunks:
                (
                    session.query(KnowledgeBaseChunk)
                    .filter(
                        KnowledgeBaseChunk.s3_key == s3_key,
                        KnowledgeBaseChunk.embedding_namespace == config.namespace,
                    )
                    .delete(synchronize_session=False)
                )

            for vector_id, metadata in vector_items:
                if not config.apply_changes:
                    progress.chunks_changed += 1
                    continue

                existing_chunk = session.query(KnowledgeBaseChunk).filter_by(vector_id=vector_id).first()
                if existing_chunk:
                    session.delete(existing_chunk)
                    session.flush()

                session.add(
                    chunk_row_from_metadata(
                        document_id=document_id,
                        vector_id=vector_id,
                        namespace=config.namespace,
                        metadata=metadata,
                    )
                )
                progress.chunks_changed += 1

            if config.apply_changes:
                file_row = session.query(KnowledgeBaseFile).filter_by(id=document_id).first()
                if file_row:
                    file_row.chunk_count = len(vector_items)

            if not config.apply_changes:
                session.rollback()

        self._emit(
            "Wrote file s3_key=%s vector_count=%s apply=%s"
            % (s3_key, len(vector_items), config.apply_changes),
            progress,
        )

    def _emit(self, message: str, progress: RAGBackfillProgress) -> None:
        self.logger.info(message)
        if self.progress_callback:
            self.progress_callback(message, progress)
