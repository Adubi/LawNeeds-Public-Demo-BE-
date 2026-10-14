"""
In-process WebSocket hub for real-time referral status updates (per user + conversation).

Note: Connections live in this process memory only. With multiple Uvicorn workers or
horizontal scaling, use sticky sessions or a shared pub/sub (e.g. Redis) so pushes
reach the worker that holds the user's socket.
"""
import json
import logging
from typing import Dict

from starlette.websockets import WebSocket

logger = logging.getLogger(__name__)


class ReferralUserStatusHub:
    """At most one waiting WebSocket per (user_id, conversation_id)."""

    def __init__(self) -> None:
        self._connections: Dict[str, WebSocket] = {}

    @staticmethod
    def _key(user_id: str, conversation_id: str) -> str:
        return f"{user_id}:{conversation_id}"

    def register(self, user_id: str, conversation_id: str, websocket: WebSocket) -> None:
        self._connections[self._key(user_id, conversation_id)] = websocket

    def unregister(self, user_id: str, conversation_id: str) -> None:
        self._connections.pop(self._key(user_id, conversation_id), None)

    async def send_update(self, user_id: str, conversation_id: str, payload: dict) -> None:
        ws = self._connections.get(self._key(user_id, conversation_id))
        if not ws:
            return
        try:
            message = {"type": "referral_user_status", **payload}
            await ws.send_text(json.dumps(message))
        except Exception as exc:
            logger.warning("referral_user_status WS send failed: %s", exc)
            self.unregister(user_id, conversation_id)


referral_user_status_hub = ReferralUserStatusHub()
