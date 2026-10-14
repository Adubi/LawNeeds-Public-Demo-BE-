"""
Secure File Download API
Provides authenticated access to S3 files via presigned URLs.

Endpoints
---------
GET /files/download/{file_id}
    Download a file attached to a single InboxMessage by its UUID.

GET /files/info/{file_id}
    Return metadata for an InboxMessage-attached file.

GET /referral/{referral_id}/attachments
    List every downloadable file that arrived with a referral request:
      • Uploaded files (voice note, PDF, DOCX, image) from ReferralRequest
      • Chat-history manuscript PDF stored as an InboxMessage

GET /referral/{referral_id}/attachments/{index}/download
    Download one item from the list above by its zero-based index.
    Generates a fresh 5-minute presigned URL and returns a 302 redirect.
"""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from typing import List, Optional, Dict, Any

from services.s3_service import S3Service
from services.models import InboxMessage, ReferralRequest
from services.db import DB
import uuid
import logging
from sqlalchemy import and_

logger = logging.getLogger(__name__)

# Presigned URL lifetime for download redirects (seconds)
_DOWNLOAD_EXPIRY = 300  # 5 minutes


def _extract_s3_key(s3_service: S3Service, url: Optional[str], metadata: Optional[Dict] = None) -> Optional[str]:
    """
    Return the S3 object key for a file, preferring the embedded key in
    upload metadata over URL extraction (which is needed when the presigned
    URL has already expired or was never stored).
    """
    if metadata and metadata.get("s3_key"):
        return metadata["s3_key"]
    return s3_service.extract_s3_key_from_url(url) if url else None


def _build_referral_file_list(
    s3_service: S3Service,
    referral: ReferralRequest,
    chat_history_messages: List[InboxMessage],
) -> List[Dict[str, Any]]:
    """
    Build a flat, zero-indexed list of every downloadable file for a referral.

    Sources (in order):
      1. attachment_metadata.files[]  — uploaded during the referral wizard
      2. Individual URL fallbacks      — voice_note_url / pdf_attachment_url /
                                         docx_attachment_url / image_attachment_url
                                         (used when metadata was not persisted, e.g.
                                         older records)
      3. InboxMessage rows with        — the chat-history PDF manuscript
         message_type='chat_history'

    Each entry contains everything the download endpoint needs to generate a
    fresh presigned URL; the 's3_key' field is the authoritative pointer.
    """
    files: List[Dict[str, Any]] = []
    seen_keys: set = set()  # deduplication guard

    def _append(
        kind: str,
        original_filename: Optional[str],
        file_type: Optional[str],
        file_size: Optional[int],
        url: Optional[str],
        metadata: Optional[Dict] = None,
    ) -> None:
        s3_key = _extract_s3_key(s3_service, url, metadata)
        dedup = s3_key or url
        if dedup and dedup in seen_keys:
            return
        if dedup:
            seen_keys.add(dedup)
        idx = len(files)
        files.append(
            {
                "index": idx,
                "kind": kind,
                "original_filename": original_filename,
                "file_type": file_type,
                "file_size": file_size,
                "s3_key": s3_key,      # kept server-side for the download handler
                "source_url": url,    # preserve the original bucket when signing
                "download_url": f"/referral/{referral.id}/attachments/{idx}/download",
            }
        )

    # --- 1. attachment_metadata.files[] ---
    meta = referral.attachment_metadata or {}
    uploaded_files = meta.get("files") or []
    for entry in uploaded_files:
        entry_meta = entry.get("metadata") or {}
        _append(
            kind=entry.get("kind", "attachment"),
            original_filename=entry.get("original_filename"),
            file_type=entry_meta.get("content_type"),
            file_size=entry_meta.get("file_size"),
            url=entry.get("url"),
            metadata=entry_meta,
        )

    # --- 2. Individual URL fallbacks (older records / single-file uploads) ---
    fallback_slots = [
        ("voice_note",  referral.voice_note_url,       meta.get("voice_note")),
        ("pdf",         referral.pdf_attachment_url,   meta.get("pdf")),
        ("docx",        referral.docx_attachment_url,  meta.get("docx")),
        ("image",       referral.image_attachment_url, meta.get("image")),
    ]
    for kind, url, slot_meta in fallback_slots:
        if url:
            _append(
                kind=kind,
                original_filename=(slot_meta or {}).get("original_filename"),
                file_type=(slot_meta or {}).get("content_type"),
                file_size=(slot_meta or {}).get("file_size"),
                url=url,
                metadata=slot_meta,
            )

    # --- 3. Chat-history manuscript PDFs from InboxMessage ---
    for msg in chat_history_messages:
        if msg.file_url:
            _append(
                kind="chat_history",
                original_filename=msg.file_name or "Chat History.pdf",
                file_type=msg.file_type or "application/pdf",
                file_size=msg.file_size,
                url=msg.file_url,
                metadata=None,
            )

    return files


class FileDownloadAPI:
    def __init__(self, *, app_name, db: DB, auth):
        self.app_name = app_name
        self.db = db
        self.auth = auth
        self.s3_service = S3Service()

    def get_router(self):
        router = APIRouter()

        # ------------------------------------------------------------------
        # Existing: download an InboxMessage file by its message UUID
        # ------------------------------------------------------------------

        @router.get("/files/download/{file_id}")
        async def download_file(file_id: str, request: Request):
            """
            Secure file download for a file attached to a single InboxMessage.
            Generates a 5-minute presigned URL and returns a 302 redirect.
            """
            try:
                token_data = self.auth.verify_request_token(request)
                user_id = token_data.get("user_id")
                if not user_id:
                    raise HTTPException(status_code=401, detail="Authentication required")

                with self.db.get_session() as session:
                    message = session.query(InboxMessage).filter(
                        InboxMessage.id == uuid.UUID(file_id)
                    ).first()

                    if not message:
                        raise HTTPException(status_code=404, detail="File not found")

                    if not message.file_url:
                        raise HTTPException(status_code=404, detail="No file attached to this message")

                    # Access control: the user who submitted the referral OR the matched lawyer
                    referral = session.query(ReferralRequest).filter(
                        ReferralRequest.id == message.referral_request_id
                    ).first()

                    if not referral:
                        raise HTTPException(status_code=404, detail="Referral not found")

                    user_uuid = uuid.UUID(user_id)
                    if referral.user_id != user_uuid and referral.matched_lawyer_id != user_uuid:
                        raise HTTPException(
                            status_code=403,
                            detail="You don't have permission to access this file",
                        )

                    s3_key = _extract_s3_key(self.s3_service, message.file_url)
                    if not s3_key:
                        raise HTTPException(
                            status_code=500,
                            detail="Could not determine file location",
                        )

                    presigned_url = self.s3_service.generate_presigned_url(
                        s3_key, expiration=_DOWNLOAD_EXPIRY, source_url=message.file_url
                    )
                    if not presigned_url:
                        raise HTTPException(
                            status_code=500, detail="Could not generate download link"
                        )

                    logger.info(f"User {user_id} downloading inbox file {file_id}")
                    return RedirectResponse(url=presigned_url, status_code=302)

            except HTTPException:
                raise
            except Exception as exc:
                logger.error(f"File download error: {exc}")
                raise HTTPException(status_code=500, detail="Internal server error")

        @router.get("/files/info/{file_id}")
        async def get_file_info(file_id: str, request: Request):
            """
            Return metadata for a file attached to a single InboxMessage.
            """
            try:
                token_data = self.auth.verify_request_token(request)
                user_id = token_data.get("user_id")
                if not user_id:
                    raise HTTPException(status_code=401, detail="Authentication required")

                with self.db.get_session() as session:
                    message = session.query(InboxMessage).filter(
                        InboxMessage.id == uuid.UUID(file_id)
                    ).first()

                    if not message:
                        raise HTTPException(status_code=404, detail="File not found")

                    if not message.file_url:
                        raise HTTPException(status_code=404, detail="No file attached to this message")

                    referral = session.query(ReferralRequest).filter(
                        ReferralRequest.id == message.referral_request_id
                    ).first()

                    if not referral:
                        raise HTTPException(status_code=404, detail="Referral not found")

                    user_uuid = uuid.UUID(user_id)
                    if referral.user_id != user_uuid and referral.matched_lawyer_id != user_uuid:
                        raise HTTPException(
                            status_code=403,
                            detail="You don't have permission to access this file",
                        )

                    return {
                        "file_id": str(message.id),
                        "file_name": message.file_name,
                        "file_type": message.file_type,
                        "file_size": message.file_size,
                        "upload_date": message.created_at.isoformat() if message.created_at else None,
                        "download_url": f"/files/download/{file_id}",
                    }

            except HTTPException:
                raise
            except Exception as exc:
                logger.error(f"File info error: {exc}")
                raise HTTPException(status_code=500, detail="Internal server error")

        # ------------------------------------------------------------------
        # New: referral attachment listing and download
        # ------------------------------------------------------------------

        @router.get("/referral/{referral_id}/attachments")
        async def list_referral_attachments(referral_id: str, request: Request):
            """
            List every downloadable file that was sent with a referral request:
              • Files uploaded during the referral wizard (voice notes, PDFs,
                DOCX documents, images)
              • The chat-history manuscript PDF delivered to the lawyer's inbox

            Accessible to the user who submitted the referral and the matched lawyer.

            Response shape
            --------------
            {
              "referral_id": "...",
              "total_count": 3,
              "files": [
                {
                  "index": 0,
                  "kind": "voice_note",
                  "original_filename": "voice_note_abc.wav",
                  "file_type": "audio/wav",
                  "file_size": 123456,
                  "download_url": "/referral/.../attachments/0/download"
                },
                {
                  "index": 1,
                  "kind": "pdf",
                  "original_filename": "legal_document.pdf",
                  "file_type": "application/pdf",
                  "file_size": 204800,
                  "download_url": "/referral/.../attachments/1/download"
                },
                {
                  "index": 2,
                  "kind": "chat_history",
                  "original_filename": "Chat History.pdf",
                  "file_type": "application/pdf",
                  "file_size": 56789,
                  "download_url": "/referral/.../attachments/2/download"
                }
              ]
            }
            """
            try:
                token_data = self.auth.verify_request_token(request)
                user_id = token_data.get("user_id")
                if not user_id:
                    raise HTTPException(status_code=401, detail="Authentication required")

                with self.db.get_session() as session:
                    referral = session.query(ReferralRequest).filter(
                        ReferralRequest.id == uuid.UUID(referral_id)
                    ).first()

                    if not referral:
                        raise HTTPException(status_code=404, detail="Referral not found")

                    user_uuid = uuid.UUID(user_id)
                    if referral.user_id != user_uuid and referral.matched_lawyer_id != user_uuid:
                        raise HTTPException(
                            status_code=403,
                            detail="You don't have access to this referral's files",
                        )

                    # Fetch chat-history InboxMessages with a file attachment
                    chat_msgs = (
                        session.query(InboxMessage)
                        .filter(
                            and_(
                                InboxMessage.referral_request_id == uuid.UUID(referral_id),
                                InboxMessage.message_type == "chat_history",
                                InboxMessage.file_url.isnot(None),
                            )
                        )
                        .order_by(InboxMessage.created_at)
                        .all()
                    )

                    files = _build_referral_file_list(self.s3_service, referral, chat_msgs)

                    # Strip internal s3_key before returning to the client
                    public_files = [
                        {k: v for k, v in f.items() if k not in {"s3_key", "source_url"}}
                        for f in files
                    ]

                    return {
                        "referral_id": referral_id,
                        "total_count": len(public_files),
                        "files": public_files,
                    }

            except HTTPException:
                raise
            except Exception as exc:
                logger.error(f"Error listing referral attachments: {exc}")
                raise HTTPException(status_code=500, detail="Internal server error")

        @router.get("/referral/{referral_id}/attachments/{index}/download")
        async def download_referral_attachment(
            referral_id: str, index: int, request: Request
        ):
            """
            Download a specific referral attachment by its zero-based index from
            GET /referral/{referral_id}/attachments.

            Generates a 5-minute presigned S3 URL and returns a 302 redirect so
            the caller receives the file directly from S3.
            """
            try:
                token_data = self.auth.verify_request_token(request)
                user_id = token_data.get("user_id")
                if not user_id:
                    raise HTTPException(status_code=401, detail="Authentication required")

                with self.db.get_session() as session:
                    referral = session.query(ReferralRequest).filter(
                        ReferralRequest.id == uuid.UUID(referral_id)
                    ).first()

                    if not referral:
                        raise HTTPException(status_code=404, detail="Referral not found")

                    user_uuid = uuid.UUID(user_id)
                    if referral.user_id != user_uuid and referral.matched_lawyer_id != user_uuid:
                        raise HTTPException(
                            status_code=403,
                            detail="You don't have access to this referral's files",
                        )

                    if index < 0:
                        raise HTTPException(status_code=400, detail="index must be >= 0")

                    # Fetch chat-history InboxMessages with a file attachment
                    chat_msgs = (
                        session.query(InboxMessage)
                        .filter(
                            and_(
                                InboxMessage.referral_request_id == uuid.UUID(referral_id),
                                InboxMessage.message_type == "chat_history",
                                InboxMessage.file_url.isnot(None),
                            )
                        )
                        .order_by(InboxMessage.created_at)
                        .all()
                    )

                    files = _build_referral_file_list(self.s3_service, referral, chat_msgs)

                    if index >= len(files):
                        raise HTTPException(
                            status_code=404,
                            detail=f"File index {index} not found. Referral has {len(files)} attachment(s).",
                        )

                    target = files[index]
                    s3_key = target.get("s3_key")

                    if not s3_key:
                        raise HTTPException(
                            status_code=500,
                            detail="File location could not be determined — the S3 key is missing",
                        )

                    presigned_url = self.s3_service.generate_presigned_url(
                        s3_key, expiration=_DOWNLOAD_EXPIRY, source_url=target.get("source_url")
                    )
                    if not presigned_url:
                        raise HTTPException(
                            status_code=500, detail="Could not generate download link"
                        )

                    logger.info(
                        "User %s downloading referral %s attachment #%d (%s) key=%s",
                        user_id, referral_id, index, target.get("kind"), s3_key,
                    )
                    return RedirectResponse(url=presigned_url, status_code=302)

            except HTTPException:
                raise
            except Exception as exc:
                logger.error(f"Error downloading referral attachment: {exc}")
                raise HTTPException(status_code=500, detail="Internal server error")

        return router
