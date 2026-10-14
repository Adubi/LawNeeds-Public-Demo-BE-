from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from langchain.text_splitter import RecursiveCharacterTextSplitter


LEGAL_CHUNKING_VERSION = "legal-aware-v1"


@dataclass
class LegalDocumentChunk:
    page_content: str
    metadata: Dict[str, Any] = field(default_factory=dict)


class LegalDocumentChunker:
    """Create legal-aware chunks from structured parser output."""

    def __init__(self, chunk_size: int = 1400, chunk_overlap: int = 180):
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.splitter = RecursiveCharacterTextSplitter(
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            length_function=len,
            separators=["\n\n", "\n", ". ", "; ", ", ", " ", ""],
        )

    def create_chunks(
        self,
        parsed_document: Dict[str, Any],
        document_metadata: Optional[Dict[str, Any]] = None,
    ) -> List[LegalDocumentChunk]:
        document_metadata = document_metadata or {}
        chunks: List[LegalDocumentChunk] = []
        document_title = (
            parsed_document.get("document_title")
            or document_metadata.get("document_title")
            or document_metadata.get("source")
            or "Untitled document"
        )

        for page in parsed_document.get("pages", []):
            page_number = page.get("page_number")
            for section_index, section in enumerate(page.get("sections", [])):
                section_heading = section.get("heading") or "Untitled section"
                body = self._section_body(section)
                if not body:
                    continue

                prefix = self._context_prefix(
                    document_title=document_title,
                    section_heading=section_heading,
                    page_number=page_number,
                    document_metadata=document_metadata,
                )
                parts = self._split_body(body, prefix)
                for part_index, part in enumerate(parts):
                    metadata = {
                        **document_metadata,
                        "document_title": document_title,
                        "section_heading": section_heading,
                        "page_number": page_number,
                        "section_index": section_index,
                        "section_chunk_index": part_index,
                        "contains_table": bool(section.get("tables")),
                        "table_count": len(section.get("tables") or []),
                        "chunking_version": LEGAL_CHUNKING_VERSION,
                    }
                    chunks.append(
                        LegalDocumentChunk(
                            page_content=f"{prefix}\n\n{part}".strip(),
                            metadata=metadata,
                        )
                    )

        for index, chunk in enumerate(chunks):
            chunk.metadata["chunk_index"] = index
            chunk.metadata["previous_chunk_index"] = index - 1 if index > 0 else None
            chunk.metadata["next_chunk_index"] = index + 1 if index + 1 < len(chunks) else None

        return chunks

    def _split_body(self, body: str, prefix: str) -> List[str]:
        if len(prefix) + len(body) <= self.chunk_size:
            return [body]
        parts = self.splitter.split_text(body)
        return [part.strip() for part in parts if part.strip()]

    @staticmethod
    def _section_body(section: Dict[str, Any]) -> str:
        parts: List[str] = []
        text = (section.get("text") or "").strip()
        if text:
            parts.append("Content:\n" + text)

        for table in section.get("tables", []) or []:
            rows = table.get("rows") or []
            if rows:
                parts.append("Table:\n" + "\n".join(LegalDocumentChunker._format_table_rows(rows)))

        return "\n\n".join(parts).strip()

    @staticmethod
    def _format_table_rows(rows: List[List[Any]]) -> List[str]:
        return [" | ".join(str(cell or "").strip() for cell in row) for row in rows]

    @staticmethod
    def _context_prefix(
        document_title: str,
        section_heading: str,
        page_number: Optional[int],
        document_metadata: Dict[str, Any],
    ) -> str:
        lines = [f"Document: {document_title}"]
        context_fields = [
            ("Practice Area", "practice_area"),
            ("Jurisdiction", "jurisdiction"),
            ("Document Type", "document_type"),
            ("Source Authority", "source_authority"),
            ("Published", "published_at"),
            ("Effective From", "effective_from"),
            ("Effective To", "effective_to"),
            ("Current", "is_current"),
            ("S3 Source", "s3_url"),
        ]
        for label, key in context_fields:
            value = document_metadata.get(key)
            if value not in (None, ""):
                lines.append(f"{label}: {value}")

        if page_number is not None:
            lines.append(f"Page: {page_number}")
        if section_heading:
            lines.append(f"Section: {section_heading}")

        return "\n".join(lines)
