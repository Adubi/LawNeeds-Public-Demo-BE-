from fastapi import APIRouter, HTTPException, Request

from services.models import User
from services.persona_service import PersonaService


class IdentityVerificationAPI:
    def __init__(self, *, app_name, db, auth):
        self.app_name = app_name
        self.db = db
        self.auth = auth
        self.persona = PersonaService()

    def get_router(self):
        router = APIRouter(prefix="/identity-verification", tags=["identity-verification"])

        @router.get("/status")
        async def status(request: Request):
            with self.db.get_session() as session:
                user = self._current_user(request, session)
                return self.persona.status_payload(session, user)

        @router.post("/start")
        async def start(request: Request):
            with self.db.get_session() as session:
                user = self._current_user(request, session, for_update=True)
                return await self.persona.start_or_resume(session, user)

        @router.post("/sync")
        async def sync(request: Request):
            with self.db.get_session() as session:
                user = self._current_user(request, session)
                return await self.persona.sync(session, user)

        @router.post("/webhooks/persona")
        async def persona_webhook(request: Request):
            raw_body = await request.body()
            signature = request.headers.get("Persona-Signature", "")
            with self.db.get_session() as session:
                return await self.persona.process_webhook(session, raw_body, signature)

        return router

    def _current_user(self, request: Request, session, for_update: bool = False) -> User:
        token_data = self.auth.verify_request_token(request)
        user_id = token_data.get("user_id")
        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid token")
        query = session.query(User).filter(User.id == user_id)
        if for_update:
            query = query.with_for_update()
        user = query.first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        return user