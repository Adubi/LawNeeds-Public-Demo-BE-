from fastapi import HTTPException, APIRouter, Request, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any
import logging
import uuid
from datetime import datetime
from services.review_service import ReviewService

# Set up logging
logger = logging.getLogger(__name__)

# Pydantic models for request/response
class SubmitReviewRequest(BaseModel):
    rating: int = Field(..., ge=1, le=5, description="Rating from 1 to 5 stars")
    review_text: Optional[str] = Field(None, description="Optional review text")
    reviewer_name: str = Field(..., min_length=1, max_length=100, description="Name to display (can be 'Anonymous')")

class ReviewResponse(BaseModel):
    id: str
    rating: int
    review_text: Optional[str]
    reviewer_name: str
    created_at: str

class ReviewWithLawyerResponse(BaseModel):
    id: str
    rating: int
    review_text: Optional[str]
    reviewer_name: str
    created_at: str
    lawyer: Dict[str, Any]

class LawyerStatsResponse(BaseModel):
    total_reviews: int
    average_rating: float

class LawyerDashboardResponse(BaseModel):
    reviews: List[ReviewResponse]
    stats: Dict[str, Any]

class SubmitReviewResponse(BaseModel):
    id: str
    message: str
    created_at: str

class DeleteReviewResponse(BaseModel):
    message: str

class ReviewsAPI:
    def __init__(self, *, app_name, db, auth):
        self.app_name = app_name
        self.db = db
        self.auth = auth
        self.review_service = ReviewService(db)

    def get_router(self):
        router = APIRouter()

        @router.post("/professionals/{professional_id}/reviews", response_model=SubmitReviewResponse)
        async def submit_review(
            request: Request,
            professional_id: str,
            review_data: SubmitReviewRequest
        ):
            """
            Legacy endpoint. Reviews are now created only from completed consultation surveys.
            """
            try:
                # Verify user token and get user_id
                token_data = self.auth.verify_request_token(request)
                user_id = token_data.get("user_id")

                logger.info(f"Received review submission from user {user_id} for lawyer {professional_id}")

                # Validate professional_id format
                try:
                    uuid.UUID(professional_id)
                except ValueError:
                    raise HTTPException(status_code=400, detail="Invalid lawyer ID format")

                # Submit review through service
                result = await self.review_service.submit_review(
                    reviewer_id=user_id,
                    lawyer_id=professional_id,
                    rating=review_data.rating,
                    review_text=review_data.review_text,
                    reviewer_name=review_data.reviewer_name
                )

                logger.info(f"Review submitted successfully: {result['id']}")
                return SubmitReviewResponse(**result)

            except ValueError as e:
                logger.warning(f"Review submission validation error: {str(e)}")
                raise HTTPException(status_code=400, detail=str(e))
            except Exception as e:
                logger.error(f"Error submitting review: {str(e)}")
                raise HTTPException(status_code=500, detail="Failed to submit review")

        @router.get("/professionals/{professional_id}/reviews", response_model=List[ReviewResponse])
        async def get_professional_reviews(
            professional_id: str,
            page: int = Query(1, ge=1, description="Page number (starts from 1)"),
            limit: int = Query(10, ge=1, le=50, description="Number of reviews per page")
        ):
            """
            Get reviews for a specific legal professional (Lawyer, Law Firm, or A2J Partner).
            
            Returns paginated list of reviews ordered by most recent first.
            """
            try:
                # Validate professional_id format
                try:
                    uuid.UUID(professional_id)
                except ValueError:
                    raise HTTPException(status_code=400, detail="Invalid lawyer ID format")

                logger.info(f"Fetching reviews for lawyer {professional_id}, page {page}, limit {limit}")

                # Get reviews through service
                reviews = self.review_service.get_lawyer_reviews(
                    lawyer_id=professional_id,
                    page=page,
                    limit=limit
                )

                return [ReviewResponse(**review) for review in reviews]

            except Exception as e:
                logger.error(f"Error getting lawyer reviews: {str(e)}")
                raise HTTPException(status_code=500, detail="Failed to get reviews")

        @router.get("/professionals/{professional_id}/stats", response_model=LawyerStatsResponse)
        async def get_professional_stats(professional_id: str):
            """
            Get review statistics for a legal professional (Lawyer, Law Firm, or A2J Partner).
            
            Returns total number of reviews and average rating.
            """
            try:
                # Validate professional_id format
                try:
                    uuid.UUID(professional_id)
                except ValueError:
                    raise HTTPException(status_code=400, detail="Invalid lawyer ID format")

                logger.info(f"Fetching stats for lawyer {professional_id}")

                # Get stats through service
                stats = self.review_service.get_lawyer_stats(lawyer_id=professional_id)

                return LawyerStatsResponse(**stats)

            except Exception as e:
                logger.error(f"Error getting lawyer stats: {str(e)}")
                raise HTTPException(status_code=500, detail="Failed to get lawyer statistics")

        @router.delete("/reviews/{review_id}", response_model=DeleteReviewResponse)
        async def delete_review(request: Request, review_id: str):
            """
            Legacy endpoint. Consultation reviews cannot be deleted from the old review flow.
            """
            raise HTTPException(
                status_code=410,
                detail="Consultation reviews cannot be managed through the legacy review endpoint"
            )

        @router.get("/users/me/reviews", response_model=List[ReviewWithLawyerResponse])
        async def get_my_reviews(
            request: Request,
            page: int = Query(1, ge=1, description="Page number (starts from 1)"),
            limit: int = Query(10, ge=1, le=50, description="Number of reviews per page")
        ):
            """
            Get reviews that you have written.
            
            Returns your reviews with lawyer information.
            """
            try:
                # Verify user token and get user_id
                token_data = self.auth.verify_request_token(request)
                user_id = token_data.get("user_id")

                logger.info(f"Fetching reviews by user {user_id}, page {page}, limit {limit}")

                # Get user reviews through service
                reviews = self.review_service.get_user_reviews(
                    user_id=user_id,
                    page=page,
                    limit=limit
                )

                return [ReviewWithLawyerResponse(**review) for review in reviews]

            except Exception as e:
                logger.error(f"Error getting user reviews: {str(e)}")
                raise HTTPException(status_code=500, detail="Failed to get your reviews")

        @router.get("/professionals/me/received-reviews", response_model=LawyerDashboardResponse)
        async def get_my_received_reviews(
            request: Request,
            page: int = Query(1, ge=1, description="Page number (starts from 1)"),
            limit: int = Query(10, ge=1, le=50, description="Number of reviews per page")
        ):
            """
            Get reviews that have been written about you (for legal professionals).
            
            Returns reviews received with detailed statistics.
            Accessible by Lawyers, Law Firms, and A2J Partners.
            """
            try:
                # Verify user token and get user_id
                token_data = self.auth.verify_request_token(request)
                user_id = token_data.get("user_id")

                logger.info(f"Fetching received reviews for lawyer {user_id}, page {page}, limit {limit}")

                # Get lawyer dashboard data through service
                dashboard_data = self.review_service.get_reviews_for_lawyer_dashboard(
                    lawyer_id=user_id,
                    page=page,
                    limit=limit
                )

                return LawyerDashboardResponse(**dashboard_data)

            except Exception as e:
                logger.error(f"Error getting lawyer dashboard reviews: {str(e)}")
                raise HTTPException(status_code=500, detail="Failed to get received reviews")


        return router
