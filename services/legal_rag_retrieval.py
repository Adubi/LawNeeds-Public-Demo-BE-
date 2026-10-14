import logging
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from langchain_core.documents import Document
from sqlalchemy import String, cast, or_

from services.models import ChatMessage, ChatResponseSource, KnowledgeBaseChunk

logger = logging.getLogger(__name__)

LEGAL_RAG_NAMESPACE = os.getenv("KNOWLEDGEBASE_EMBEDDING_NAMESPACE", "legal-aware-v1")
LEGAL_RAG_RETRIEVAL_ENABLED = os.getenv("LEGAL_RAG_RETRIEVAL_ENABLED", "true").lower() != "false"
INTERNAL_STORAGE_HOSTS = (
    "amazonaws.com",
    "digitaloceanspaces.com",
)

LATEST_QUERY_TERMS = {
    "latest",
    "current",
    "recent",
    "updated",
    "up to date",
    "up-to-date",
    "valid now",
    "in force",
    "today",
    "new guidance",
    "newest",
}

LEGAL_AREA_PATTERNS = {
    "Immigration Law": r"\b(immigration|visa|asylum|citizenship|deportation|sponsor|skilled worker|ilr|leave to remain|home office)\b",
    "Family Law": r"\b(divorce|custody|child arrangement|family law|matrimonial|domestic abuse|financial remedy)\b",
    "Employment Law": r"\b(employment|dismissal|redundancy|discrimination|harassment|acas|worker|employee|tribunal)\b",
    "Road Traffic Offences": r"\b(driving|traffic|speeding|drink driving|totting|points|licence|license|road traffic)\b",
}

DOCUMENT_TYPE_PATTERNS = {
    "guidance": r"\b(guidance|policy|guide|latest|current|updated)\b",
    "case": r"\b(case|judgment|judgement|court|decision|appeal|ruling)\b",
    "legislation": r"\b(act|regulation|rules|statute|legislation|order)\b",
}


@dataclass
class LegalRetrievalResult:
    documents: List[Document]
    context: str
    sources: List[Dict[str, Any]]
    intent: Dict[str, Any]


class LegalRAGRetrievalService:
    """Freshness-aware hybrid retrieval for structured legal knowledge-base chunks."""

    def __init__(
        self,
        *,
        index,
        embedding_function,
        db,
        namespace: str = LEGAL_RAG_NAMESPACE,
        candidate_k: int = 18,
        final_k: int = 5,
        max_context_chars: int = 12000,
    ):
        self.index = index
        self.embedding_function = embedding_function
        self.db = db
        self.namespace = namespace
        self.candidate_k = candidate_k
        self.final_k = final_k
        self.max_context_chars = max_context_chars

    def classify_query(self, question: str) -> Dict[str, Any]:
        question_lower = (question or "").lower()
        wants_latest = any(term in question_lower for term in LATEST_QUERY_TERMS)

        practice_area = None
        for area, pattern in LEGAL_AREA_PATTERNS.items():
            if re.search(pattern, question_lower):
                practice_area = area
                break

        document_type = None
        for candidate_type, pattern in DOCUMENT_TYPE_PATTERNS.items():
            if re.search(pattern, question_lower):
                document_type = candidate_type
                break

        jurisdiction = "UK" if re.search(r"\b(uk|britain|england|wales|scotland|home office|acas)\b", question_lower) else None

        return {
            "wants_latest": wants_latest,
            "practice_area": practice_area,
            "document_type": document_type,
            "jurisdiction": jurisdiction,
            "keywords": self._query_terms(question),
        }

    def retrieve(self, question: str, k: Optional[int] = None) -> LegalRetrievalResult:
        target_k = k or self.final_k
        intent = self.classify_query(question)

        semantic_candidates = self._semantic_candidates(question, intent, max(target_k * 3, self.candidate_k))
        keyword_candidates = self._keyword_candidates(question, intent, max(target_k * 3, self.candidate_k))
        ranked_candidates = self._rerank_candidates(question, intent, semantic_candidates, keyword_candidates)
        selected = ranked_candidates[:target_k]

        documents = [self._candidate_to_document(candidate, index + 1) for index, candidate in enumerate(selected)]
        sources = [doc.metadata.get("source_reference") for doc in documents if doc.metadata.get("source_reference")]
        context = self.build_context(documents)

        return LegalRetrievalResult(
            documents=documents,
            context=context,
            sources=sources,
            intent=intent,
        )

    def _semantic_candidates(self, question: str, intent: Dict[str, Any], top_k: int) -> List[Dict[str, Any]]:
        if not self.index:
            return []

        try:
            query_embedding = self.embedding_function.embed_query(question)
            response = self.index.query(
                vector=query_embedding,
                top_k=top_k,
                include_metadata=True,
                namespace=self.namespace,
                filter=self._pinecone_filter(intent),
            )
            matches = self._response_matches(response)
            return [self._candidate_from_match(match) for match in matches]
        except Exception as exc:
            logger.warning(f"Legal semantic retrieval failed: {exc}")
            return []

    def _keyword_candidates(self, question: str, intent: Dict[str, Any], limit: int) -> List[Dict[str, Any]]:
        terms = intent.get("keywords") or []
        if not terms:
            return []

        with self.db.get_session() as session:
            query = self._chunk_projection(session).filter(
                KnowledgeBaseChunk.embedding_namespace == self.namespace,
                KnowledgeBaseChunk.is_current.is_(True),
            )
            if intent.get("practice_area"):
                query = query.filter(KnowledgeBaseChunk.practice_area == intent["practice_area"])
            if intent.get("document_type"):
                query = query.filter(KnowledgeBaseChunk.document_type == intent["document_type"])
            if intent.get("jurisdiction"):
                query = query.filter(KnowledgeBaseChunk.jurisdiction == intent["jurisdiction"])

            search_filters = []
            for term in terms[:8]:
                like_term = f"%{term}%"
                search_filters.extend(
                    [
                        KnowledgeBaseChunk.chunk_text.ilike(like_term),
                        KnowledgeBaseChunk.document_title.ilike(like_term),
                        KnowledgeBaseChunk.section_heading.ilike(like_term),
                    ]
                )
            if search_filters:
                query = query.filter(or_(*search_filters))

            rows = query.order_by(
                KnowledgeBaseChunk.published_at.desc().nullslast(),
                KnowledgeBaseChunk.updated_at.desc().nullslast(),
            ).limit(limit * 4).all()

            candidates = []
            for row in rows:
                haystack = " ".join(
                    [
                        row.chunk_text or "",
                        row.document_title or "",
                        row.section_heading or "",
                    ]
                ).lower()
                keyword_score = self._keyword_score(terms, haystack)
                if keyword_score <= 0:
                    continue
                candidates.append(self._candidate_from_chunk(row, keyword_score=keyword_score))

        candidates.sort(key=lambda candidate: candidate.get("keyword_score", 0), reverse=True)
        return candidates[:limit]

    def _rerank_candidates(
        self,
        question: str,
        intent: Dict[str, Any],
        semantic_candidates: List[Dict[str, Any]],
        keyword_candidates: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        merged: Dict[str, Dict[str, Any]] = {}
        for candidate in semantic_candidates + keyword_candidates:
            vector_id = candidate.get("vector_id")
            if not vector_id:
                continue
            existing = merged.get(vector_id, {})
            merged[vector_id] = {**candidate, **existing}
            merged[vector_id]["semantic_score"] = max(
                float(existing.get("semantic_score") or 0),
                float(candidate.get("semantic_score") or 0),
            )
            merged[vector_id]["keyword_score"] = max(
                float(existing.get("keyword_score") or 0),
                float(candidate.get("keyword_score") or 0),
            )

        ranked = []
        for candidate in merged.values():
            candidate["rerank_score"] = self._rerank_score(candidate, intent)
            ranked.append(candidate)

        ranked.sort(key=lambda candidate: candidate["rerank_score"], reverse=True)
        return ranked

    def _rerank_score(self, candidate: Dict[str, Any], intent: Dict[str, Any]) -> float:
        score = 0.0
        score += min(float(candidate.get("semantic_score") or 0), 1.0) * 0.55
        score += min(float(candidate.get("keyword_score") or 0), 1.0) * 0.25

        if candidate.get("is_current") is True:
            score += 0.08

        if intent.get("practice_area") and candidate.get("practice_area") == intent["practice_area"]:
            score += 0.05
        if intent.get("document_type") and candidate.get("document_type") == intent["document_type"]:
            score += 0.04

        if intent.get("wants_latest"):
            score += self._freshness_score(candidate) * 0.20

        if candidate.get("source_authority"):
            score += 0.03
        if candidate.get("page_number"):
            score += 0.02
        if candidate.get("section_heading"):
            score += 0.02

        return round(score, 6)

    def _freshness_score(self, candidate: Dict[str, Any]) -> float:
        date_value = candidate.get("published_at") or candidate.get("effective_from")
        parsed = self._parse_datetime(date_value)
        if not parsed:
            return 0.0

        now = datetime.now(timezone.utc)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        age_days = max((now - parsed).days, 0)
        return max(0.0, 1.0 - (age_days / 730.0))

    def _candidate_to_document(self, candidate: Dict[str, Any], source_number: int) -> Document:
        source_id = f"S{source_number}"
        source_reference = self._source_reference(candidate, source_id)
        metadata = {
            **candidate,
            "source_id": source_id,
            "source_reference": source_reference,
        }
        page_content = self._context_block(candidate, source_id)
        return Document(page_content=page_content, metadata=metadata)

    def build_context(self, documents: List[Document]) -> str:
        blocks = []
        current_size = 0
        for doc in documents:
            block = doc.page_content
            if current_size + len(block) > self.max_context_chars:
                break
            blocks.append(block)
            current_size += len(block)
        return "\n\n---\n\n".join(blocks)

    def format_sources_for_response(self, sources: List[Dict[str, Any]]) -> str:
        if not sources:
            return ""

        lines = ["\n\n## Sources"]
        for raw_source in sources:
            source = self.sanitize_source_reference(raw_source)
            parts = [
                f"[{source.get('source_id')}]",
                source.get("title") or "Untitled source",
            ]
            if source.get("section"):
                parts.append(f"section \"{source['section']}\"")
            if source.get("page"):
                parts.append(f"p. {source['page']}")
            if source.get("published_at"):
                parts.append(f"published {source['published_at']}")
            if source.get("source_authority"):
                parts.append(source["source_authority"])
            lines.append(f"- {', '.join(str(part) for part in parts if part)}")
            public_url = self.public_resource_url(source.get("source_url"))
            if public_url:
                lines.append(f"  Resource: {public_url}")
        return "\n".join(lines)

    def persist_response_sources(
        self,
        *,
        conversation_id: str,
        user_id: str,
        message_index: int,
        sources: List[Dict[str, Any]],
    ) -> int:
        if not sources:
            return 0

        with self.db.get_session() as session:
            message = session.query(cast(ChatMessage.id, String).label("id")).filter(
                ChatMessage.conversation_id == uuid.UUID(conversation_id),
                ChatMessage.user_id == uuid.UUID(user_id),
                ChatMessage.message_index == message_index,
                ChatMessage.role == "assistant",
            ).order_by(ChatMessage.created_at.desc()).first()
            if not message:
                logger.warning(f"Assistant message not found for source persistence: {conversation_id}:{message_index}")
                return 0

            rows = []
            for raw_source in sources:
                source = self.sanitize_source_reference(raw_source)
                rows.append(
                    ChatResponseSource(
                        conversation_id=uuid.UUID(conversation_id),
                        message_id=uuid.UUID(str(message.id)),
                        user_id=uuid.UUID(user_id),
                        document_id=self._safe_uuid(source.get("document_id")),
                        chunk_id=self._safe_uuid(source.get("chunk_db_id")),
                        vector_id=source.get("vector_id"),
                        similarity_score=source.get("similarity_score"),
                        rerank_score=source.get("rerank_score"),
                        source_title=source.get("title"),
                        source_section=source.get("section"),
                        source_page=source.get("page"),
                        source_url=self.public_resource_url(source.get("source_url")),
                        source_authority=source.get("source_authority"),
                        document_type=source.get("document_type"),
                        jurisdiction=source.get("jurisdiction"),
                        practice_area=source.get("practice_area"),
                        published_at=self._parse_datetime(source.get("published_at_raw")),
                        effective_from=self._parse_datetime(source.get("effective_from_raw")),
                        effective_to=self._parse_datetime(source.get("effective_to_raw")),
                        is_current=source.get("is_current", True),
                        source_metadata=source,
                    )
                )
            session.add_all(rows)
            session.commit()
            return len(rows)

    def _pinecone_filter(self, intent: Dict[str, Any]) -> Dict[str, Any]:
        filters: Dict[str, Any] = {"is_current": {"$eq": True}}
        if intent.get("practice_area"):
            filters["practice_area"] = {"$eq": intent["practice_area"]}
        if intent.get("document_type"):
            filters["document_type"] = {"$eq": intent["document_type"]}
        if intent.get("jurisdiction"):
            filters["jurisdiction"] = {"$eq": intent["jurisdiction"]}
        return filters

    def _response_matches(self, response) -> List[Any]:
        if isinstance(response, dict):
            return response.get("matches") or []
        return getattr(response, "matches", []) or []

    def _candidate_from_match(self, match) -> Dict[str, Any]:
        metadata = self._match_metadata(match)
        vector_id = self._match_value(match, "id") or metadata.get("id")
        score = float(self._match_value(match, "score") or 0.0)
        candidate = {
            **metadata,
            "vector_id": vector_id,
            "chunk_text": metadata.get("text") or metadata.get("chunk_text") or "",
            "semantic_score": score,
            "keyword_score": 0.0,
        }
        chunk_record = self._chunk_by_vector_id(vector_id)
        if chunk_record:
            semantic_score = candidate["semantic_score"]
            keyword_score = candidate["keyword_score"]
            candidate.update(chunk_record)
            candidate["semantic_score"] = semantic_score
            candidate["keyword_score"] = keyword_score
        return candidate

    def _candidate_from_chunk(self, row: KnowledgeBaseChunk, keyword_score: float = 0.0) -> Dict[str, Any]:
        return {
            "chunk_db_id": str(row.id),
            "document_id": str(row.document_id),
            "vector_id": row.vector_id,
            "chunk_text": self.strip_internal_storage_urls(row.chunk_text),
            "document_title": row.document_title,
            "section_heading": row.section_heading,
            "page_number": row.page_number,
            "s3_url": row.s3_url,
            "source_url": self.public_resource_url(row.source_url),
            "source_authority": row.source_authority,
            "document_type": row.document_type,
            "jurisdiction": row.jurisdiction,
            "practice_area": row.practice_area,
            "published_at": row.published_at,
            "effective_from": row.effective_from,
            "effective_to": row.effective_to,
            "is_current": row.is_current,
            "semantic_score": 0.0,
            "keyword_score": keyword_score,
        }

    def _chunk_by_vector_id(self, vector_id: Optional[str]) -> Dict[str, Any]:
        if not vector_id:
            return {}
        with self.db.get_session() as session:
            row = self._chunk_projection(session).filter(
                KnowledgeBaseChunk.vector_id == vector_id,
                KnowledgeBaseChunk.embedding_namespace == self.namespace,
            ).first()
            if not row:
                return {}
            return self._candidate_from_chunk(row)

    def _chunk_projection(self, session):
        return session.query(
            cast(KnowledgeBaseChunk.id, String).label("id"),
            cast(KnowledgeBaseChunk.document_id, String).label("document_id"),
            KnowledgeBaseChunk.vector_id.label("vector_id"),
            KnowledgeBaseChunk.chunk_text.label("chunk_text"),
            KnowledgeBaseChunk.document_title.label("document_title"),
            KnowledgeBaseChunk.section_heading.label("section_heading"),
            KnowledgeBaseChunk.page_number.label("page_number"),
            KnowledgeBaseChunk.s3_url.label("s3_url"),
            KnowledgeBaseChunk.source_url.label("source_url"),
            KnowledgeBaseChunk.source_authority.label("source_authority"),
            KnowledgeBaseChunk.document_type.label("document_type"),
            KnowledgeBaseChunk.jurisdiction.label("jurisdiction"),
            KnowledgeBaseChunk.practice_area.label("practice_area"),
            KnowledgeBaseChunk.published_at.label("published_at"),
            KnowledgeBaseChunk.effective_from.label("effective_from"),
            KnowledgeBaseChunk.effective_to.label("effective_to"),
            KnowledgeBaseChunk.is_current.label("is_current"),
        )

    def _source_reference(self, candidate: Dict[str, Any], source_id: str) -> Dict[str, Any]:
        published_at = self._parse_datetime(candidate.get("published_at"))
        effective_from = self._parse_datetime(candidate.get("effective_from"))
        effective_to = self._parse_datetime(candidate.get("effective_to"))
        return {
            "source_id": source_id,
            "vector_id": candidate.get("vector_id"),
            "chunk_db_id": candidate.get("chunk_db_id"),
            "document_id": candidate.get("document_id") or candidate.get("file_id"),
            "title": candidate.get("document_title") or candidate.get("title"),
            "section": candidate.get("section_heading"),
            "page": candidate.get("page_number"),
            "source_url": self.public_resource_url(candidate.get("source_url")),
            "source_authority": candidate.get("source_authority"),
            "document_type": candidate.get("document_type"),
            "jurisdiction": candidate.get("jurisdiction"),
            "practice_area": candidate.get("practice_area"),
            "published_at": published_at.date().isoformat() if published_at else None,
            "published_at_raw": published_at.isoformat() if published_at else None,
            "effective_from_raw": effective_from.isoformat() if effective_from else None,
            "effective_to_raw": effective_to.isoformat() if effective_to else None,
            "is_current": candidate.get("is_current", True),
            "similarity_score": candidate.get("semantic_score"),
            "rerank_score": candidate.get("rerank_score"),
        }

    def _context_block(self, candidate: Dict[str, Any], source_id: str) -> str:
        published_at = self._parse_datetime(candidate.get("published_at"))
        lines = [
            f"[{source_id}]",
            f"Document: {candidate.get('document_title') or candidate.get('title') or 'Untitled'}",
        ]
        if candidate.get("practice_area"):
            lines.append(f"Practice Area: {candidate['practice_area']}")
        if candidate.get("jurisdiction"):
            lines.append(f"Jurisdiction: {candidate['jurisdiction']}")
        if candidate.get("document_type"):
            lines.append(f"Document Type: {candidate['document_type']}")
        if candidate.get("source_authority"):
            lines.append(f"Source Authority: {candidate['source_authority']}")
        public_url = self.public_resource_url(candidate.get("source_url"))
        if public_url:
            lines.append(f"Source URL: {public_url}")
        if published_at:
            lines.append(f"Published: {published_at.date().isoformat()}")
        if candidate.get("section_heading"):
            lines.append(f"Section: {candidate['section_heading']}")
        if candidate.get("page_number"):
            lines.append(f"Page: {candidate['page_number']}")
        lines.append("")
        lines.append("Content:")
        lines.append(self.strip_internal_storage_urls(candidate.get("chunk_text") or ""))
        return "\n".join(lines)

    @staticmethod
    def public_resource_url(url: Optional[str]) -> Optional[str]:
        if not url:
            return None

        parsed = urlparse(str(url).strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return None

        host = parsed.netloc.lower()
        if host.startswith("s3.") or ".s3." in host:
            return None
        if any(host == suffix or host.endswith(f".{suffix}") for suffix in INTERNAL_STORAGE_HOSTS):
            return None

        return url

    @classmethod
    def sanitize_source_reference(cls, source: Dict[str, Any]) -> Dict[str, Any]:
        sanitized = dict(source or {})
        sanitized["source_url"] = cls.public_resource_url(sanitized.get("source_url"))
        sanitized.pop("s3_url", None)
        return sanitized

    @classmethod
    def strip_internal_storage_urls(cls, text: Optional[str]) -> str:
        if not text:
            return ""

        def replace_url(match):
            url = match.group(0).rstrip(").,;]")
            suffix = match.group(0)[len(url):]
            return suffix if cls.public_resource_url(url) is None else match.group(0)

        return re.sub(r"https?://[^\s<>\"]+", replace_url, str(text))

    def _query_terms(self, question: str) -> List[str]:
        words = re.findall(r"[a-zA-Z][a-zA-Z0-9\-]{2,}", question or "")
        stop_words = {
            "the",
            "and",
            "for",
            "with",
            "that",
            "this",
            "what",
            "how",
            "can",
            "does",
            "are",
            "latest",
            "current",
            "legal",
            "guidance",
        }
        terms = []
        for word in words:
            lowered = word.lower()
            if lowered not in stop_words and lowered not in terms:
                terms.append(lowered)
        return terms

    def _keyword_score(self, terms: List[str], haystack: str) -> float:
        if not terms:
            return 0.0
        matches = sum(1 for term in terms if term in haystack)
        return min(matches / max(len(terms), 1), 1.0)

    def _match_metadata(self, match) -> Dict[str, Any]:
        if isinstance(match, dict):
            return dict(match.get("metadata") or {})
        return dict(getattr(match, "metadata", {}) or {})

    def _match_value(self, match, key: str) -> Any:
        if isinstance(match, dict):
            return match.get(key)
        return getattr(match, key, None)

    def _parse_datetime(self, value) -> Optional[datetime]:
        if isinstance(value, datetime):
            return value
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value))
        except ValueError:
            return None

    def _safe_uuid(self, value):
        if not value:
            return None
        try:
            return uuid.UUID(str(value))
        except (ValueError, TypeError):
            return None
