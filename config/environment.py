import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv


_DOTENV_LOADED = False

APP_ENV_ALIASES = {
    "local": "local",
    "dev": "dev",
    "development": "dev",
    "prod": "prod",
    "production": "prod",
}

PAYMENTS_ENV_ALIASES = {
    "sandbox": "sandbox",
    "test": "sandbox",
    "testing": "sandbox",
    "dev": "sandbox",
    "development": "sandbox",
    "live": "live",
    "prod": "live",
    "production": "live",
}


def load_environment() -> None:
    """Load backend .env once without overriding real environment variables."""
    global _DOTENV_LOADED
    if _DOTENV_LOADED:
        return

    env_path = Path(__file__).resolve().parents[1] / ".env"
    load_dotenv(dotenv_path=env_path, override=False)
    _DOTENV_LOADED = True


def _normalize(value: Optional[str], aliases: dict[str, str], default: str) -> str:
    if not value:
        return default
    return aliases.get(value.strip().lower(), default)


def get_app_env() -> str:
    load_environment()
    return _normalize(
        os.getenv("ENVIRONMENT") or os.getenv("APP_ENV"),
        APP_ENV_ALIASES,
        "local",
    )


def get_payments_env() -> str:
    load_environment()
    payments_env = _normalize(
        os.getenv("PAYMENTS_ENV"),
        PAYMENTS_ENV_ALIASES,
        "sandbox",
    )
    if (
        get_app_env() == "prod"
        and payments_env != "live"
        and os.getenv("ALLOW_SANDBOX_PAYMENTS_IN_PROD", "").strip().lower()
        not in {"1", "true", "yes"}
    ):
        return "live"
    return payments_env


def is_live_payments() -> bool:
    return get_payments_env() == "live"


def get_stripe_webhook_secret(*, connect: bool = False) -> Optional[str]:
    """Resolve the Stripe webhook secret for platform or Connect events."""
    load_environment()

    mode = "PROD" if is_live_payments() else "DEV"
    scope = "CONNECT" if connect else "PLATFORM"
    preferred_key = f"STRIPE_WEBHOOK_SECRET_{mode}_{scope}"
    fallback_keys = (
        ("STRIPE_CONNECT_WEBHOOK_SECRET", "STRIPE_WEBHOOK_SECRET")
        if connect
        else ("STRIPE_WEBHOOK_SECRET",)
    )

    return os.getenv(preferred_key) or next(
        (os.getenv(key) for key in fallback_keys if os.getenv(key)),
        None,
    )


def get_revenuecat_webhook_secret() -> Optional[str]:
    """Resolve the RevenueCat webhook secret for sandbox or live billing."""
    load_environment()

    mode = "LIVE" if is_live_payments() else "SANDBOX"
    return os.getenv(f"REVENUECAT_WEBHOOK_SECRET_{mode}") or os.getenv(
        "REVENUECAT_WEBHOOK_SECRET"
    )
