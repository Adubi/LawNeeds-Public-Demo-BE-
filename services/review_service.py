import logging
import uuid
from typing import List, Dict, Optional, Tuple
from datetime import datetime, timezone
from sqlalchemy.orm import Session
from sqlalchemy import desc, func
from services.models import LawyerReview, LawyerReviewStats, User, DeviceToken
from services.notification_service import NotificationService
from services.firebase_notification_service import FirebaseNotificationService

logger = logging.getLogger(__name__)

class ReviewService:
    """
    Service for managing reviews and statistics for legal professionals
    (Lawyers, Law Firms, and A2J Partners)
    """
    
    def __init__(self, db):
        self.db = db
        self.firebase_service = FirebaseNotificationService()
    
    async def submit_review(self, reviewer_id: str, lawyer_id: str, rating: int, 
                     review_text: Optional[str], reviewer_name: str) -> Dict:
        """
        Submit a new review for a lawyer
        Returns: Dictionary with review details or raises exception
        """
        try:
            # Validate rating
            if not 1 <= rating <= 5:
                raise ValueError("Rating must be between 1 and 5")
            
            with self.db.get_session() as session:
                # Check if professional exists and is reviewable
                professional = session.query(User).filter(User.id == uuid.UUID(lawyer_id)).first()
                if not professional:
                    raise ValueError("Professional not found")
                
                # Check if user has a reviewable professional type
                reviewable_types = ["Lawyer", "Law Firm", "A2J Partners"]
                if professional.professional_type not in reviewable_types:
                    logger.warning(f"User {lawyer_id} being reviewed but professional_type is: {professional.professional_type}")
                    # Still allow the review but log it
                
                # Check if user already reviewed this professional
                existing_review = session.query(LawyerReview).filter(
                    LawyerReview.reviewer_id == uuid.UUID(reviewer_id),
                    LawyerReview.lawyer_id == uuid.UUID(lawyer_id)
                ).first()
                
                if existing_review:
                    raise ValueError("You have already reviewed this professional. Delete your existing review to submit a new one.")
                
                # Create new review
                review = LawyerReview(
                    reviewer_id=uuid.UUID(reviewer_id),
                    lawyer_id=uuid.UUID(lawyer_id),
                    rating=rating,
                    review_text=review_text,
                    reviewer_name=reviewer_name
                )
                session.add(review)
                session.flush()  # Get the ID
                
                # Update stats
                self._update_lawyer_stats(session, lawyer_id)
                
                # Notify the lawyer of the new review (in-app, best-effort)
                try:
                    NotificationService(session).create(
                        recipient_id=uuid.UUID(lawyer_id),
                        title="You have a new review",
                        body=f"{reviewer_name} left you a {rating}-star review",
                        notification_type="new_review",
                        data={
                            "review_id": str(review.id),
                            "rating": rating,
                            "reviewer_name": reviewer_name,
                        },
                    )
                except Exception as notify_error:
                    logger.error(f"Failed to send new_review notification for lawyer {lawyer_id}: {str(notify_error)}")
                
                # Send push notification to the lawyer (best-effort)
                try:
                    lawyer_devices = session.query(DeviceToken).filter(
                        DeviceToken.user_id == uuid.UUID(lawyer_id),
                        DeviceToken.is_active == True
                    ).all()
                    if lawyer_devices:
                        device_tokens = [device.device_token for device in lawyer_devices]
                        await self.firebase_service.send_new_review_notification(
                            device_tokens=device_tokens,
                            reviewer_name=reviewer_name,
                            rating=rating,
                            review_id=str(review.id),
                        )
                except Exception as push_error:
                    logger.error(f"Failed to send new_review push notification for lawyer {lawyer_id}: {str(push_error)}")
                
                logger.info(f"Review submitted: {review.id} for lawyer {lawyer_id} by user {reviewer_id}")
                
                return {
                    "id": str(review.id),
                    "message": "Review submitted successfully",
                    "created_at": review.created_at.isoformat()
                }
                
        except Exception as e:
            logger.error(f"Error submitting review: {str(e)}")
            raise
    
    def get_lawyer_reviews(self, lawyer_id: str, page: int = 1, limit: int = 10) -> List[Dict]:
        """
        Get paginated reviews for a lawyer
        Returns: List of review dictionaries
        """
        try:
            with self.db.get_session() as session:
                offset = (page - 1) * limit
                
                reviews = session.query(LawyerReview).filter(
                    LawyerReview.lawyer_id == uuid.UUID(lawyer_id)
                ).order_by(LawyerReview.created_at.desc()).offset(offset).limit(limit).all()
                
                return [
                    {
                        "id": str(review.id),
                        "rating": review.rating,
                        "review_text": review.review_text,
                        "reviewer_name": review.reviewer_name,
                        "created_at": review.created_at.isoformat()
                    }
                    for review in reviews
                ]
                
        except Exception as e:
            logger.error(f"Error getting lawyer reviews: {str(e)}")
            raise
    
    def get_lawyer_stats(self, lawyer_id: str) -> Dict:
        """
        Get review statistics for a lawyer
        Returns: Dictionary with total_reviews and average_rating
        """
        try:
            with self.db.get_session() as session:
                stats = session.query(LawyerReviewStats).filter(
                    LawyerReviewStats.lawyer_id == uuid.UUID(lawyer_id)
                ).first()
                
                if not stats:
                    return {"total_reviews": 0, "average_rating": 0.0}
                
                return {
                    "total_reviews": stats.total_reviews,
                    "average_rating": float(stats.average_rating)
                }
                
        except Exception as e:
            logger.error(f"Error getting lawyer stats: {str(e)}")
            raise
    
    def delete_review(self, review_id: str, user_id: str) -> Dict:
        """
        Delete a user's own review
        Returns: Success message or raises exception
        """
        try:
            with self.db.get_session() as session:
                review = session.query(LawyerReview).filter(
                    LawyerReview.id == uuid.UUID(review_id),
                    LawyerReview.reviewer_id == uuid.UUID(user_id)
                ).first()
                
                if not review:
                    raise ValueError("Review not found or you don't have permission to delete it")
                
                lawyer_id = str(review.lawyer_id)
                session.delete(review)
                session.flush()
                
                # Update stats
                self._update_lawyer_stats(session, lawyer_id)
                
                logger.info(f"Review deleted: {review_id} by user {user_id}")
                
                return {"message": "Review deleted successfully. You can now submit a new review."}
                
        except Exception as e:
            logger.error(f"Error deleting review: {str(e)}")
            raise
    
    def get_user_reviews(self, user_id: str, page: int = 1, limit: int = 10) -> List[Dict]:
        """
        Get reviews written by a specific user
        Returns: List of review dictionaries with lawyer info
        """
        try:
            with self.db.get_session() as session:
                offset = (page - 1) * limit
                
                reviews = session.query(LawyerReview, User).join(
                    User, LawyerReview.lawyer_id == User.id
                ).filter(
                    LawyerReview.reviewer_id == uuid.UUID(user_id)
                ).order_by(LawyerReview.created_at.desc()).offset(offset).limit(limit).all()
                
                return [
                    {
                        "id": str(review.id),
                        "rating": review.rating,
                        "review_text": review.review_text,
                        "reviewer_name": review.reviewer_name,
                        "created_at": review.created_at.isoformat(),
                        "lawyer": {
                            "id": str(lawyer.id),
                            "name": f"{lawyer.first_name} {lawyer.last_name}".strip(),
                            "firm_name": lawyer.firm_name or lawyer.professional_employer
                        }
                    }
                    for review, lawyer in reviews
                ]
                
        except Exception as e:
            logger.error(f"Error getting user reviews: {str(e)}")
            raise
    
    def get_reviews_for_lawyer_dashboard(self, lawyer_id: str, page: int = 1, limit: int = 10) -> Dict:
        """
        Get reviews for a lawyer's dashboard with additional stats
        Returns: Dictionary with reviews and statistics
        """
        try:
            with self.db.get_session() as session:
                # Get reviews
                offset = (page - 1) * limit
                reviews = session.query(LawyerReview).filter(
                    LawyerReview.lawyer_id == uuid.UUID(lawyer_id)
                ).order_by(LawyerReview.created_at.desc()).offset(offset).limit(limit).all()
                
                # Get detailed stats
                stats = session.query(LawyerReviewStats).filter(
                    LawyerReviewStats.lawyer_id == uuid.UUID(lawyer_id)
                ).first()
                
                # Get rating distribution
                rating_distribution = session.query(
                    LawyerReview.rating,
                    func.count(LawyerReview.rating).label('count')
                ).filter(
                    LawyerReview.lawyer_id == uuid.UUID(lawyer_id)
                ).group_by(LawyerReview.rating).all()
                
                rating_counts = {i: 0 for i in range(1, 6)}
                for rating, count in rating_distribution:
                    rating_counts[rating] = count
                
                return {
                    "reviews": [
                        {
                            "id": str(review.id),
                            "rating": review.rating,
                            "review_text": review.review_text,
                            "reviewer_name": review.reviewer_name,
                            "created_at": review.created_at.isoformat()
                        }
                        for review in reviews
                    ],
                    "stats": {
                        "total_reviews": stats.total_reviews if stats else 0,
                        "average_rating": float(stats.average_rating) if stats else 0.0,
                        "rating_distribution": rating_counts
                    }
                }
                
        except Exception as e:
            logger.error(f"Error getting lawyer dashboard reviews: {str(e)}")
            raise
    
    def _update_lawyer_stats(self, session: Session, lawyer_id: str):
        """
        Update or create lawyer review statistics
        """
        try:
            # Calculate new stats
            result = session.query(
                func.count(LawyerReview.id).label('total'),
                func.avg(LawyerReview.rating).label('average')
            ).filter(LawyerReview.lawyer_id == uuid.UUID(lawyer_id)).first()
            
            total_reviews = result.total or 0
            average_rating = float(result.average or 0)
            
            # Update or create stats record
            stats = session.query(LawyerReviewStats).filter(
                LawyerReviewStats.lawyer_id == uuid.UUID(lawyer_id)
            ).first()
            
            if stats:
                stats.total_reviews = total_reviews
                stats.average_rating = average_rating
                stats.last_updated = datetime.now(timezone.utc)
            else:
                stats = LawyerReviewStats(
                    lawyer_id=uuid.UUID(lawyer_id),
                    total_reviews=total_reviews,
                    average_rating=average_rating
                )
                session.add(stats)
            
            logger.debug(f"Updated stats for lawyer {lawyer_id}: {total_reviews} reviews, {average_rating:.2f} avg rating")
            
        except Exception as e:
            logger.error(f"Error updating lawyer stats: {str(e)}")
            raise