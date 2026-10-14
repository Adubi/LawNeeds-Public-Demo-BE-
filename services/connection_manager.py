"""
Shared in-process WebSocket connection manager.

Exposes a singleton `inbox_manager` that is imported by both the WebSocket
handler and the REST send endpoints so that HTTP-triggered messages are
broadcast to any connected WebSocket clients in real time.

Note: connections live in this process only. For multi-worker / horizontally
scaled deployments, replace the in-memory dicts with a Redis pub/sub layer.
"""
from __future__ import annotations

import logging
from typing import Dict, Set

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class ConnectionManager:
    """Manage WebSocket connections keyed by user_id."""

    def __init__(self) -> None:
        self.active_connections: Dict[str, WebSocket] = {}
        self.session_participants: Dict[str, Set[str]] = {}
        self.inbox_participants: Dict[str, Set[str]] = {}

    async def connect(
        self,
        websocket: WebSocket,
        user_id: str,
        connection_id: str,
        connection_type: str = "session",
    ) -> None:
        await websocket.accept()
        self.active_connections[user_id] = websocket

        if connection_type == "session":
            self.session_participants.setdefault(connection_id, set()).add(user_id)
        elif connection_type == "inbox":
            self.inbox_participants.setdefault(connection_id, set()).add(user_id)

        logger.info("User %s connected to %s %s", user_id, connection_type, connection_id)

    def disconnect(
        self,
        user_id: str,
        connection_id: str,
        connection_type: str = "session",
    ) -> None:
        self.active_connections.pop(user_id, None)

        if connection_type == "session":
            participants = self.session_participants.get(connection_id)
            if participants is not None:
                participants.discard(user_id)
                if not participants:
                    del self.session_participants[connection_id]
        elif connection_type == "inbox":
            participants = self.inbox_participants.get(connection_id)
            if participants is not None:
                participants.discard(user_id)
                if not participants:
                    del self.inbox_participants[connection_id]

        logger.info("User %s disconnected from %s %s", user_id, connection_type, connection_id)

    async def send_personal_message(self, message: str, user_id: str) -> None:
        ws = self.active_connections.get(user_id)
        if ws:
            await ws.send_text(message)

    async def broadcast_to_session(
        self, message: str, session_id: str, exclude_user: str | None = None
    ) -> None:
        for user_id in list(self.session_participants.get(session_id, [])):
            if user_id != exclude_user:
                ws = self.active_connections.get(user_id)
                if ws:
                    try:
                        await ws.send_text(message)
                    except Exception as exc:
                        logger.warning("broadcast_to_session failed for %s: %s", user_id, exc)

    async def broadcast_to_inbox(
        self, message: str, referral_id: str, exclude_user: str | None = None
    ) -> None:
        for user_id in list(self.inbox_participants.get(referral_id, [])):
            if user_id != exclude_user:
                ws = self.active_connections.get(user_id)
                if ws:
                    try:
                        await ws.send_text(message)
                    except Exception as exc:
                        logger.warning("broadcast_to_inbox failed for %s: %s", user_id, exc)


# ---------------------------------------------------------------------------
# Singletons — import these everywhere instead of constructing new instances
# ---------------------------------------------------------------------------
inbox_manager: ConnectionManager = ConnectionManager()
