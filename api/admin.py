import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func

from services.models import HomeActionEvent, User


logger = logging.getLogger(__name__)


class SearchVisibilityUpdate(BaseModel):
    is_search_visible: bool
    user_id: Optional[uuid.UUID] = None
    email: Optional[str] = Field(default=None, min_length=3)
    reason: Optional[str] = Field(default=None, max_length=500)


class AdminAPI:
    def __init__(self, *, app_name, db):
        self.app_name = app_name
        self.db = db

    def get_router(self):
        router = APIRouter(prefix="/admin", tags=["admin"])

        @router.patch("/users/search-visibility")
        async def update_user_search_visibility(
            payload: SearchVisibilityUpdate,
            request: Request,
            x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token"),
        ):
            self._require_admin_token(x_admin_token)

            if not payload.user_id and not payload.email:
                raise HTTPException(status_code=400, detail="Provide user_id or email")

            with self.db.get_session() as session:
                query = session.query(User)

                if payload.user_id:
                    query = query.filter(User.id == payload.user_id)
                else:
                    query = query.filter(func.lower(User.email) == payload.email.strip().lower())

                user = query.first()
                if not user:
                    raise HTTPException(status_code=404, detail="User not found")

                if not user.professional_type:
                    raise HTTPException(
                        status_code=400,
                        detail="Search visibility only applies to professional accounts",
                    )

                previous_value = user.is_search_visible
                user.is_search_visible = payload.is_search_visible
                session.flush()

                logger.info(
                    "admin_search_visibility_updated",
                    extra={
                        "target_user_id": str(user.id),
                        "target_email": user.email,
                        "professional_type": user.professional_type,
                        "previous_is_search_visible": previous_value,
                        "new_is_search_visible": payload.is_search_visible,
                        "reason": payload.reason,
                        "client_host": request.client.host if request.client else None,
                    },
                )

                return {
                    "id": str(user.id),
                    "email": user.email,
                    "first_name": user.first_name,
                    "last_name": user.last_name,
                    "professional_type": user.professional_type,
                    "is_search_visible": user.is_search_visible,
                }

        @router.get("/analytics/home-actions")
        async def get_home_action_analytics(
            days: int = Query(default=30, ge=1, le=365),
            recent_limit: int = Query(default=50, ge=1, le=200),
            x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token"),
        ):
            """Return Home Page action totals and recent selections for the Admin Dashboard."""
            self._require_admin_token(x_admin_token)
            since = datetime.now(timezone.utc) - timedelta(days=days)

            with self.db.get_session() as session:
                grouped = (
                    session.query(HomeActionEvent.action, func.count(HomeActionEvent.id))
                    .filter(HomeActionEvent.created_at >= since)
                    .group_by(HomeActionEvent.action)
                    .all()
                )
                counts = {action: count for action, count in grouped}

                recent = (
                    session.query(HomeActionEvent, User.email)
                    .join(User, User.id == HomeActionEvent.user_id)
                    .filter(HomeActionEvent.created_at >= since)
                    .order_by(HomeActionEvent.created_at.desc())
                    .limit(recent_limit)
                    .all()
                )

                return {
                    "period_days": days,
                    "total": sum(counts.values()),
                    "counts": {
                        "ask_lawneeds_ai": counts.get("ask_lawneeds_ai", 0),
                        "find_professional": counts.get("find_professional", 0),
                        "book_consultation": counts.get("book_consultation", 0),
                    },
                    "recent": [
                        {
                            "id": str(event.id),
                            "user_id": str(event.user_id),
                            "email": email,
                            "action": event.action,
                            "destination": event.destination,
                            "platform": event.platform,
                            "app_version": event.app_version,
                            "created_at": event.created_at.isoformat(),
                        }
                        for event, email in recent
                    ],
                }

        return router

    @staticmethod
    def _require_admin_token(provided_token: Optional[str]) -> None:
        expected_token = os.getenv("ADMIN_API_TOKEN")
        if not expected_token:
            raise HTTPException(status_code=503, detail="Admin API token is not configured")
        if not provided_token or provided_token != expected_token:
            raise HTTPException(status_code=401, detail="Invalid admin token")
