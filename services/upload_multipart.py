"""Helpers for collecting and classifying multiple multipart file uploads."""
from __future__ import annotations

import hashlib
import mimetypes
from typing import Any, Dict, List, Optional, Sequence, Tuple

from fastapi import HTTPException
from starlette.datastructures import UploadFile


MULTIPART_FILE_FIELD_NAMES: Tuple[str, ...] = (
    "files",
    "attachments",
    "file",
    "attachment",
    # Common field names used by mobile / web clients for audio recordings
    "recording",
    "voice",
    "voice_note",
    "audio",
)


def infer_referral_file_type(
    content_type: Optional[str],
    filename: Optional[str],
    file_head: Optional[bytes] = None,
) -> str:
    """Map MIME / extension / magic bytes to referral S3 file_type: voice_note, pdf, docx, image.

    Multipart clients often send wrong Content-Type (e.g. image/jpeg for a *.pdf). Filename
    and PDF magic bytes are consulted before trusting image/* MIME.
    """
    ct = (content_type or "").lower().strip()
    name = (filename or "").lower()

    if ct.startswith("audio/"):
        return "voice_note"

    # Trust document extensions before image/* so mislabeled PDFs/DOCs are not stored as images.
    if name.endswith(".pdf"):
        return "pdf"
    if name.endswith(".docx") or name.endswith(".doc"):
        return "docx"

    if file_head and len(file_head) >= 5 and file_head[:5] == b"%PDF-":
        return "pdf"

    if ct == "application/pdf":
        return "pdf"
    if ct in (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/msword",
    ):
        return "docx"
    if ct.startswith("image/"):
        return "image"

    ext = name.rsplit(".", 1)[-1] if "." in name else ""
    if ext in ("jpg", "jpeg", "png", "gif", "bmp", "webp", "tiff", "tif", "svg"):
        return "image"
    if ext in ("mp3", "wav", "m4a", "ogg", "webm", "aac", "mpeg"):
        return "voice_note"

    return "pdf"


def try_pdf_document_title(pdf_bytes: bytes) -> Optional[str]:
    """Return PDF /Title from document metadata when present and meaningful."""
    if not pdf_bytes:
        return None
    try:
        import fitz
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        raw = doc.metadata.get("title") if doc.metadata else None
        doc.close()
        if raw is None:
            return None
        title = str(raw).strip().strip("\x00")
        if title.startswith("\ufeff"):
            title = title[1:].strip()
        if not title:
            return None
        lower = title.lower()
        junk = (
            "untitled",
            "title",
            "open pdf",
            "no title",
            "microsoft word",
        )
        if lower in junk or lower.startswith("microsoft word - "):
            return None
        return title
    except Exception:
        return None


def public_attachment_display_name(
    *,
    kind: str,
    metadata: Optional[Dict[str, Any]] = None,
    original_filename: Optional[str] = None,
    placeholder: Optional[str] = None,
) -> str:
    """User-visible label: embedded PDF title, then stored filename, then placeholder."""
    meta = metadata if isinstance(metadata, dict) else {}
    doc_title = meta.get("document_title")
    if doc_title and str(doc_title).strip():
        return str(doc_title).strip()
    for key in ("original_filename", "name", "file_name"):
        v = meta.get(key)
        if v and str(v).strip():
            s = str(v).strip()
            if s.lower() in ("open pdf",):
                continue
            return s
    if original_filename and str(original_filename).strip():
        return str(original_filename).strip()
    if placeholder:
        return placeholder
    return kind or "Attachment"


def content_sha256_hex(data: bytes) -> str:
    """SHA-256 hex digest of file bytes (stored for reference only, not used for dedup)."""
    return hashlib.sha256(data).hexdigest()


def _name_label_from_uploaded_entry(entry: Dict[str, Any]) -> Tuple[Optional[str], str]:
    """Return (normalized filename or None, human label) for a referral uploaded_files item."""
    meta = entry.get("metadata")
    raw: Optional[str] = None
    if isinstance(meta, dict):
        raw = (
            entry.get("original_filename")
            or meta.get("original_filename")
            or meta.get("name")
        )
    else:
        raw = entry.get("original_filename")
    label = raw or "previous upload"
    if raw:
        return str(raw).lower().strip(), str(label)
    return None, ""


def names_from_referral_context(context: Dict[str, Any]) -> Dict[str, str]:
    """Map normalized filename -> display label for files already in referral flow context."""
    out: Dict[str, str] = {}
    for e in context.get("uploaded_files") or []:
        if not isinstance(e, dict):
            continue
        name, label = _name_label_from_uploaded_entry(e)
        if name:
            out[name] = label
    am = context.get("attachment_metadata")
    if isinstance(am, dict):
        out.update(names_from_attachment_metadata_dict(am))
    return out


def names_from_attachment_metadata_dict(am: Dict[str, Any]) -> Dict[str, str]:
    """Collect normalized filename -> label from persisted ReferralRequest.attachment_metadata."""
    out: Dict[str, str] = {}
    for f in am.get("files") or []:
        if not isinstance(f, dict):
            continue
        meta = f.get("metadata") or {}
        raw = (
            f.get("original_filename")
            or meta.get("original_filename")
            or meta.get("name")
        )
        if not raw:
            continue
        out[str(raw).lower().strip()] = str(raw)
    for key in ("pdf", "docx", "image", "voice_note"):
        block = am.get(key)
        if not isinstance(block, dict):
            continue
        raw = block.get("original_filename") or (block.get("upload_result") or {}).get("original_filename")
        if not raw:
            continue
        out[str(raw).lower().strip()] = str(raw)
    return out


def check_duplicate_filename(
    filename: str,
    existing_labels: Dict[str, str],
    batch_labels: Dict[str, str],
) -> None:
    """Raise HTTP 409 if filename matches a prior attachment or another file in the same batch."""
    key = (filename or "").lower().strip()
    if not key:
        return
    if key in existing_labels:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "duplicate_file",
                "message": (
                    f"A file named \"{filename}\" was already attached "
                    f"({existing_labels[key]}). Please rename or choose a different file."
                ),
                "existing_label": existing_labels[key],
                "filename": filename,
            },
        )
    if key in batch_labels:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "duplicate_file",
                "message": (
                    f"A file named \"{filename}\" appears more than once in this upload "
                    f"({batch_labels[key]}). Please remove the duplicate."
                ),
                "existing_label": batch_labels[key],
                "filename": filename,
            },
        )


def collect_upload_files_from_form(form: Any, field_names: Sequence[str] = MULTIPART_FILE_FIELD_NAMES) -> List[UploadFile]:
    """Gather UploadFile instances from a Starlette FormData (dedupe by object id)."""
    collected: List[UploadFile] = []
    seen: set = set()
    for key in field_names:
        for item in form.getlist(key):
            if isinstance(item, UploadFile) and getattr(item, "filename", None):
                i = id(item)
                if i not in seen:
                    seen.add(i)
                    collected.append(item)
    return collected


def referral_upload_entry(kind: str, upload_result: Dict[str, Any], original_filename: Optional[str]) -> Dict[str, Any]:
    """Normalized entry for referral context attachment_metadata['files']."""
    entry: Dict[str, Any] = {
        "kind": kind,
        "url": upload_result.get("url"),
        "original_filename": original_filename,
        "metadata": upload_result,
    }
    sha = upload_result.get("content_sha256")
    if sha:
        entry["content_sha256"] = sha
    return entry


def merge_referral_upload_into_context(context: Dict[str, Any], kind: str, upload_result: Dict[str, Any], original_filename: Optional[str]) -> None:
    """Append to uploaded_files and set legacy single-slot URLs (first of each kind wins)."""
    entry = referral_upload_entry(kind, upload_result, original_filename)
    files = context.setdefault("uploaded_files", [])
    files.append(entry)

    url = upload_result.get("url")
    if not url:
        return

    if kind == "voice_note":
        context.setdefault("voice_note_url", url)
        context.setdefault("voice_note_metadata", upload_result)
    elif kind == "pdf":
        context.setdefault("pdf_attachment_url", url)
        context.setdefault("pdf_metadata", upload_result)
    elif kind == "docx":
        context.setdefault("docx_attachment_url", url)
        context.setdefault("docx_metadata", upload_result)
    elif kind == "image":
        context.setdefault("image_attachment_url", url)
        context.setdefault("image_metadata", upload_result)


def build_referral_attachment_metadata(context: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Build attachment_metadata dict for ReferralRequest from flow context."""
    uploaded = context.get("uploaded_files") or []
    meta: Dict[str, Any] = {}
    if uploaded:
        meta["files"] = uploaded
    if context.get("voice_note_metadata"):
        meta["voice_note"] = context.get("voice_note_metadata")
    if context.get("pdf_metadata"):
        meta["pdf"] = context.get("pdf_metadata")
    if context.get("docx_metadata"):
        meta["docx"] = context.get("docx_metadata")
    if context.get("image_metadata"):
        meta["image"] = context.get("image_metadata")
    return meta if meta else None


def _sequential_label(kind: str, index: int) -> str:
    """Return a sequential label for an attachment."""
    mapping = {
        "voice_note": str(index),
        "pdf":        f"pdf{index}",
        "docx":       f"docx{index}",
        "image":      f"image{index}",
    }
    return mapping.get(kind, f"{kind}{index}")


def _attachment_display_title(entry: Dict[str, Any], kind: str, index: int) -> str:
    """Return the best available title for an uploaded_files entry.

    - Voice notes always use sequential labels (their filenames are UUID-based
      and meaningless to the user).
    - For PDFs and DOCX, prefer the embedded document title, then the original
      filename supplied by the user.
    - For images, prefer the original filename, then fall back to a sequential
      label.
    """
    if kind == "voice_note":
        return _sequential_label(kind, index)

    meta = entry.get("metadata") if isinstance(entry.get("metadata"), dict) else {}

    # Embedded document title (PDFs only)
    doc_title = meta.get("document_title") or ""
    if doc_title.strip():
        return doc_title.strip()

    # User-supplied filename
    for key in ("original_filename", "file_name", "name"):
        v = entry.get(key) or meta.get(key) or ""
        if v and str(v).strip():
            candidate = str(v).strip()
            # Filter out generic/unhelpful auto-generated names
            if not (candidate.lower().startswith("voice_note_") and len(candidate) > 30):
                return candidate

    # Last resort: sequential label
    return _sequential_label(kind, index)


def attachment_confirmation_lines(context: Dict[str, Any]) -> str:
    """Human-readable attachment summary for referral confirmation.

    Each attachment is listed by its display title rather than showing
    transcription text or a generic 'Attached' placeholder:
        Voice note: 1
        PDF document: Employment Contract.pdf
        Image: image1
    """
    uf = context.get("uploaded_files") or []
    parts: List[str] = []

    if uf:
        # Counters per kind for sequential numbering (1-based)
        kind_counters: Dict[str, int] = {}
        kind_labels = {
            "voice_note": "Voice note",
            "pdf":        "PDF document",
            "docx":       "Word document",
            "image":      "Image",
        }
        for entry in uf:
            if not isinstance(entry, dict):
                continue
            kind = entry.get("kind") or "file"
            kind_counters[kind] = kind_counters.get(kind, 0) + 1
            idx = kind_counters[kind]
            title = _attachment_display_title(entry, kind, idx)
            label = kind_labels.get(kind, kind.replace("_", " ").capitalize())
            parts.append(f"\n{label}: {title}")
        return "".join(parts)

    # Legacy single-URL context fields (kept for backward compatibility)
    if context.get("voice_note_url"):
        parts.append("\nVoice note: 1")
    if context.get("pdf_attachment_url"):
        fname = context.get("pdf_attachment_filename", "").strip()
        parts.append(f"\nPDF document: {fname or 'pdf001'}")
    if context.get("docx_attachment_url"):
        fname = context.get("docx_attachment_filename", "").strip()
        parts.append(f"\nWord document: {fname or 'docx001'}")
    if context.get("image_attachment_url"):
        fname = context.get("image_attachment_filename", "").strip()
        parts.append(f"\nImage: {fname or 'image001'}")
    return "".join(parts)


def inbox_mime_to_category(content_type: Optional[str], filename: Optional[str]) -> str:
    """Broad category for inbox messages: document | image | audio | other."""
    ct = (content_type or "").lower()
    if ct.startswith("audio/"):
        return "audio"
    if ct.startswith("image/"):
        return "image"
    if ct in (
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/msword",
    ):
        return "document"
    guess, _ = mimetypes.guess_type(filename or "")
    if guess:
        if guess.startswith("image/"):
            return "image"
        if guess.startswith("audio/"):
            return "audio"
    return "other"
