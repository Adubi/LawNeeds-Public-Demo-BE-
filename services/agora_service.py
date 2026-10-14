"""
Agora token generation — Phase 1 of LawNeeds in-app voice/video calling.

Generates short-lived RTC tokens server-side (Section 6.1 of the calling
scope doc). The App Certificate never leaves this file / the backend.
"""

import os
import time

from agora_token_builder import RtcTokenBuilder

AGORA_APP_ID = os.getenv("AGORA_APP_ID")
AGORA_APP_CERTIFICATE = os.getenv("AGORA_APP_CERTIFICATE")

# How long an issued token stays valid, in seconds.
TOKEN_EXPIRATION_SECONDS = 3600

# Publisher role — used for both participants since both send/receive
# audio/video in a 1:1 consultation call.
ROLE_PUBLISHER = 1


def generate_rtc_token(channel_name: str) -> str:
    """
    Generate a short-lived Agora RTC token scoped to one channel.

    uid=0 is used deliberately: it means "don't authenticate a specific
    numeric uid," so the token authorizes anyone holding it to join this
    one channel — access control is enforced by who we hand the token
    to (Section 6.4's server-side participant checks), not by uid.
    """
    if not AGORA_APP_ID or not AGORA_APP_CERTIFICATE:
        raise RuntimeError("AGORA_APP_ID / AGORA_APP_CERTIFICATE not configured")

    expire_ts = int(time.time()) + TOKEN_EXPIRATION_SECONDS

    return RtcTokenBuilder.buildTokenWithUid(
        AGORA_APP_ID,
        AGORA_APP_CERTIFICATE,
        channel_name,
        0,
        ROLE_PUBLISHER,
        expire_ts,
    )