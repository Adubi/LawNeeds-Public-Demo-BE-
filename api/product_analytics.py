import logging
import uuid
from enum import Enum
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from services.models import HomeActionEvent, User


logger = logging.getLogger(__name__)


class HomeAction(str, Enum):
    ASK_LAWNEEDS_AI = "ask_lawneeds_ai"
    FIND_PROFESSIONAL = "find_professional"
    BOOK_CONSULTATION = "book_consultation"


ACTION_DESTINATIONS = {
    HomeAction.ASK_LAWNEEDS_AI: "chatbot",
    HomeAction.FIND_PROFESSIONAL: "search",
    HomeAction.BOOK_CONSULTATION: "chatbot_referral",
}


class HomeActionEventCreate(BaseModel):
    action: HomeAction
    platform: Optional[str] = Field(default=None, max_length=20)
    app_version: Optional[str] = Field(default=None, max_length=30)


class ProductAnalyticsAPI:
    def __init__(self, *, app_name, db, auth):
        self.app_name = app_name
        self.db = db
        self.auth = auth

    def get_router(self):
        router = APIRouter(prefix="/analytics", tags=["analytics"])

        @router.post("/home-actions", status_code=status.HTTP_201_CREATED)
        async def record_home_action(payload: HomeActionEventCreate, request: Request):
            token_data = self.auth.verify_request_token(request)
            user_id = token_data.get("user_id")
            if not user_id:
                raise HTTPException(status_code=401, detail="Invalid token")

            try:
                parsed_user_id = uuid.UUID(str(user_id))
            except (TypeError, ValueError):
                raise HTTPException(status_code=401, detail="Invalid token")

            with self.db.get_session() as session:
                if not session.query(User.id).filter(User.id == parsed_user_id).first():
                    raise HTTPException(status_code=404, detail="User not found")

                event = HomeActionEvent(
                    user_id=parsed_user_id,
                    action=payload.action.value,
                    destination=ACTION_DESTINATIONS[payload.action],
                    platform=payload.platform,
                    app_version=payload.app_version,
                )
                session.add(event)
                session.flush()

                logger.info(
                    "home_action_selected",
                    extra={
                        "user_id": str(parsed_user_id),
                        "action": event.action,
                        "destination": event.destination,
                        "platform": event.platform,
                        "app_version": event.app_version,
                    },
                )

                return {
                    "id": str(event.id),
                    "action": event.action,
                    "destination": event.destination,
                    "created_at": event.created_at.isoformat(),
                }

        return router
