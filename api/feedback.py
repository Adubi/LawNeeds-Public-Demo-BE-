from fastapi import HTTPException, APIRouter, File, UploadFile, Form, Request, BackgroundTasks
from typing import List, Optional
import logging
import os
import uuid
import json
from datetime import datetime
from pydantic import BaseModel
from services.models import BetaFeedback
from services.email import EmailService
from sqlalchemy import func
import traceback

# Set up logging
logger = logging.getLogger(__name__)

class FeedbackResponse(BaseModel):
    id: str
    message: str
    created_at: str

class FeedbackAPI:
    def __init__(self, *, app_name, db, auth):
        self.app_name = app_name
        self.db = db
        self.auth = auth
        self.router = APIRouter()

    def get_router(self):
        router = APIRouter()

        @router.post("/feedback/submit", response_model=FeedbackResponse)
        async def submit_feedback(
            background_tasks: BackgroundTasks,
            request: Request,
            name: str = Form(..., description="Full name of the person submitting feedback"),
            email: str = Form(..., description="Email address"),
            phone: Optional[str] = Form(None, description="Phone number (optional)"),
            comment: str = Form(..., description="Feedback comment"),
            photo: Optional[UploadFile] = File(None, description="Optional legacy single photo upload")
        ):
            """
            Submit beta feedback with optional image uploads.

            This endpoint accepts:
            - name: Required field for the person's name
            - email: Required email address
            - phone: Optional phone number
            - comment: Required feedback comment
            - photo/photos: Optional image file upload(s)

            NOTE: this is also the trigger point for the app's "Contact
            Support" screen (app/(app)/manage/support.tsx) - it submits
            to this same endpoint via UserService.SubmitFeedback. There
            is no separate support-ticket system, so this single
            submission fires two distinct emails: one to the internal
            team (_send_feedback_email, unchanged) and one to the user
            confirming receipt (_send_support_confirmation_email, new -
            automation #12). Different audiences, not a duplicate.
            """
            try:
                logger.info(f"Received feedback submission from {email}")
                
                # Validate required fields
                if not name or not name.strip():
                    raise HTTPException(status_code=400, detail="Name is required")
                if not email or not email.strip():
                    raise HTTPException(status_code=400, detail="Email is required")
                if not comment or not comment.strip():
                    raise HTTPException(status_code=400, detail="Comment is required")
                
                form = await request.form()
                photo_uploads: List[UploadFile] = []
                seen_upload_ids = set()

                for item in [photo, *form.getlist("photo"), *form.getlist("photos")]:
                    is_upload = (
                        item is not None
                        and getattr(item, "filename", None)
                        and hasattr(item, "read")
                    )
                    if is_upload and id(item) not in seen_upload_ids:
                        photo_uploads.append(item)
                        seen_upload_ids.add(id(item))

                saved_photo_paths = []
                saved_photo_filenames = []

                if photo_uploads:
                    allowed_extensions = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp'}
                    max_size = 10 * 1024 * 1024  # 10MB per image
                    upload_dir = f"volume/{self.app_name}/uploads/feedback"
                    os.makedirs(upload_dir, exist_ok=True)

                    for upload in photo_uploads:
                        file_extension = os.path.splitext(upload.filename)[1].lower()

                        if file_extension not in allowed_extensions:
                            raise HTTPException(
                                status_code=400,
                                detail=f"Invalid file type. Allowed types: {', '.join(allowed_extensions)}"
                            )

                        photo_content = await upload.read()

                        if len(photo_content) > max_size:
                            raise HTTPException(
                                status_code=400,
                                detail=f"{upload.filename} exceeds 10MB limit"
                            )

                        unique_filename = f"{uuid.uuid4()}{file_extension}"
                        saved_path = os.path.join(upload_dir, unique_filename)

                        with open(saved_path, "wb") as buffer:
                            buffer.write(photo_content)

                        saved_photo_paths.append(saved_path)
                        saved_photo_filenames.append(upload.filename)
                        logger.info(f"Photo saved to {saved_path}")

                photo_path = (
                    saved_photo_paths[0]
                    if len(saved_photo_paths) == 1
                    else json.dumps(saved_photo_paths) if saved_photo_paths else None
                )
                photo_filename = (
                    saved_photo_filenames[0]
                    if len(saved_photo_filenames) == 1
                    else json.dumps(saved_photo_filenames) if saved_photo_filenames else None
                )
                
                # Create feedback record
                with self.db.get_session() as session:
                    feedback = BetaFeedback(
                        name=name.strip(),
                        email=email.strip().lower(),
                        phone=phone.strip() if phone else None,
                        comment=comment.strip(),
                        photo_path=photo_path,
                        photo_filename=photo_filename
                    )
                    
                    session.add(feedback)
                    session.commit()
                    
                    logger.info(f"Feedback saved with ID: {feedback.id}")
                    
                    # Send internal notification email in the background (unchanged)
                    background_tasks.add_task(
                        self._send_feedback_email,
                        feedback_id=str(feedback.id),
                        name=name.strip(),
                        email=email.strip(),
                        phone=phone.strip() if phone else "Not provided",
                        comment=comment.strip(),
                        photo_filename=photo_filename,
                        created_at=feedback.created_at
                    )

                    # Send user-facing "we've received your request" confirmation
                    # in the background (automation #12). Separate audience from
                    # the internal notification above - not a duplicate.
                    background_tasks.add_task(
                        self._send_support_confirmation_email,
                        feedback_id=str(feedback.id),
                        name=name.strip(),
                        email=email.strip(),
                    )
                    
                    return FeedbackResponse(
                        id=str(feedback.id),
                        message="Feedback submitted successfully",
                        created_at=feedback.created_at.isoformat()
                    )
                    
            except HTTPException:
                # Re-raise HTTP exceptions
                raise
            except Exception as e:
                logger.error(f"Error in submit_feedback: {str(e)}")
                logger.error(traceback.format_exc())
                raise HTTPException(status_code=500, detail="Internal server error")

        @router.get("/feedback/list")
        async def list_feedback(
            page: int = 1,
            limit: int = 50
        ):
            """
            List all feedback submissions (admin endpoint).
            
            Query parameters:
            - page: Page number (default: 1)
            - limit: Items per page (default: 50, max: 100)
            """
            try:
                # Validate pagination parameters
                if page < 1:
                    raise HTTPException(status_code=400, detail="Page must be >= 1")
                if limit < 1 or limit > 100:
                    raise HTTPException(status_code=400, detail="Limit must be between 1 and 100")
                
                with self.db.get_session() as session:
                    # Calculate offset
                    offset = (page - 1) * limit
                    
                    # Get total count
                    total_count = session.query(func.count(BetaFeedback.id)).scalar()
                    
                    # Get feedback records
                    feedback_records = (
                        session.query(BetaFeedback)
                        .order_by(BetaFeedback.created_at.desc())
                        .offset(offset)
                        .limit(limit)
                        .all()
                    )
                    
                    # Format response
                    feedback_list = []
                    for feedback in feedback_records:
                        feedback_data = {
                            "id": str(feedback.id),
                            "name": feedback.name,
                            "email": feedback.email,
                            "phone": feedback.phone,
                            "comment": feedback.comment,
                            "has_photo": bool(feedback.photo_path),
                            "photo_filename": feedback.photo_filename,
                            "photo_filenames": self._parse_feedback_photo_field(feedback.photo_filename),
                            "created_at": feedback.created_at.isoformat(),
                            "updated_at": feedback.updated_at.isoformat()
                        }
                        feedback_list.append(feedback_data)
                    
                    return {
                        "feedback": feedback_list,
                        "pagination": {
                            "page": page,
                            "limit": limit,
                            "total": total_count,
                            "total_pages": (total_count + limit - 1) // limit
                        }
                    }
                    
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error in list_feedback: {str(e)}")
                raise HTTPException(status_code=500, detail="Internal server error")

        return router

    def _parse_feedback_photo_field(self, value: Optional[str]) -> List[str]:
        if not value:
            return []
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else [str(parsed)]
        except (TypeError, ValueError):
            return [value]

    def _send_feedback_email(self, feedback_id: str, name: str, email: str, phone: str, 
                           comment: str, photo_filename: Optional[str], created_at: datetime):
        """
        Send email notification for new feedback submission
        """
        try:
            subject = f"🚀 New Beta Feedback Received - {name}"
            
            # Format the submission time
            submission_time = created_at.strftime("%B %d, %Y at %I:%M %p UTC")
            photo_filenames = self._parse_feedback_photo_field(photo_filename)
            photo_label = ", ".join(photo_filenames)
            
            # Create HTML content
            html_content = f"""
            <html>
            <head>
                <style>
                    body {{ font-family: Arial, sans-serif; line-height: 1.6; color: #333; }}
                    .container {{ max-width: 600px; margin: 0 auto; padding: 20px; background-color: #f9f9f9; }}
                    .header {{ background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); 
                              color: white; padding: 20px; border-radius: 10px 10px 0 0; text-align: center; }}
                    .content {{ background: white; padding: 20px; border-radius: 0 0 10px 10px; 
                               box-shadow: 0 2px 10px rgba(0,0,0,0.1); }}
                    .field {{ margin-bottom: 15px; }}
                    .field-label {{ font-weight: bold; color: #667eea; margin-bottom: 5px; }}
                    .field-value {{ background: #f8f9fa; padding: 10px; border-radius: 5px; 
                                   border-left: 3px solid #667eea; }}
                    .comment {{ background: #fff3cd; border: 1px solid #ffeaa7; padding: 15px; 
                               border-radius: 8px; margin: 10px 0; }}
                    .metadata {{ background: #e3f2fd; padding: 10px; border-radius: 5px; 
                                font-size: 12px; color: #666; margin-top: 20px; }}
                    .photo-notice {{ background: #d4edda; color: #155724; padding: 10px; 
                                    border-radius: 5px; border: 1px solid #c3e6cb; }}
                    .emoji {{ font-size: 18px; }}
                </style>
            </head>
            <body>
                <div class="container">
                    <div class="header">
                        <h2><span class="emoji">🚀</span> New Beta Feedback Received</h2>
                        <p>Someone has submitted feedback for LawNeeds!</p>
                    </div>
                    <div class="content">
                        <div class="field">
                            <div class="field-label"><span class="emoji">👤</span> Name:</div>
                            <div class="field-value">{name}</div>
                        </div>
                        
                        <div class="field">
                            <div class="field-label"><span class="emoji">📧</span> Email:</div>
                            <div class="field-value">{email}</div>
                        </div>
                        
                        <div class="field">
                            <div class="field-label"><span class="emoji">📱</span> Phone:</div>
                            <div class="field-value">{phone}</div>
                        </div>
                        
                        <div class="field">
                            <div class="field-label"><span class="emoji">💬</span> Feedback:</div>
                            <div class="comment">{comment}</div>
                        </div>
                        
                        {f'''<div class="field">
                            <div class="field-label"><span class="emoji">📷</span> Photo Attachment:</div>
                            <div class="photo-notice">
                                <strong>{len(photo_filenames)} photo{'s' if len(photo_filenames) != 1 else ''} attached:</strong> {photo_label}<br>
                                <small>The photo has been saved to the server and can be accessed through the admin panel.</small>
                            </div>
                        </div>''' if photo_filenames else ''}
                        
                        <div class="metadata">
                            <strong>Feedback ID:</strong> {feedback_id}<br>
                            <strong>Submitted:</strong> {submission_time}<br>
                            <strong>Source:</strong> LawNeeds Beta Feedback Form
                        </div>
                    </div>
                </div>
            </body>
            </html>
            """
            
            # Create plain text version
            text_content = f"""
            NEW BETA FEEDBACK RECEIVED
            
            Name: {name}
            Email: {email}
            Phone: {phone}
            
            Feedback:
            {comment}
            
            {f'Photo Attachment(s): {photo_label}' if photo_filenames else 'No photo attached'}
            
            Feedback ID: {feedback_id}
            Submitted: {submission_time}
            Source: LawNeeds Beta Feedback Form
            """
            
            # Send email to info@lawneeds.co.uk
            success = EmailService.send_email(
                to_email="info@lawneeds.co.uk",
                subject=subject,
                html_content=html_content,
                text_content=text_content
            )
            
            if success:
                logger.info(f"Feedback notification email sent successfully for feedback ID: {feedback_id}")
            else:
                logger.error(f"Failed to send feedback notification email for feedback ID: {feedback_id}")
                
        except Exception as e:
            logger.error(f"Error sending feedback email notification: {str(e)}")
            logger.error(traceback.format_exc())

    def _send_support_confirmation_email(self, feedback_id: str, name: str, email: str):
        """
        Send the user-facing "we've received your support request"
        confirmation email (automation #12). Wrapped in its own
        try/except, same defensive pattern as #7-#11, so a failure here
        never affects the already-committed feedback submission or the
        internal notification email above.
        """
        try:
            success = EmailService.send_support_request_received_email(
                to_email=email,
                first_name=name,
                feedback_id=feedback_id,
            )

            if success:
                logger.info(f"Support confirmation email sent successfully for feedback ID: {feedback_id}")
            else:
                logger.error(f"Failed to send support confirmation email for feedback ID: {feedback_id}")

        except Exception as e:
            logger.error(f"Error sending support confirmation email: {str(e)}")
            logger.error(traceback.format_exc())