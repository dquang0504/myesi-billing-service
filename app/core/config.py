import os
import json
from dotenv import load_dotenv

load_dotenv()


class Settings:
    STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
    STRIPE_PUBLIC_KEY = os.getenv("STRIPE_PUBLIC_KEY")
    STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
    PADDLE_API_KEY = os.getenv("PADDLE_API_KEY", "")
    PADDLE_WEBHOOK_SECRET = os.getenv("PADDLE_WEBHOOK_SECRET", "")
    PADDLE_ENV = os.getenv("PADDLE_ENV", "sandbox")
    _paddle_api_base = os.getenv("PADDLE_API_BASE")
    if not _paddle_api_base:
        if os.getenv("PADDLE_ENV", "sandbox").lower() == "sandbox":
            _paddle_api_base = "https://sandbox.api.paddle.com"
        else:
            _paddle_api_base = "https://api.paddle.com"
    PADDLE_API_BASE = _paddle_api_base
    PADDLE_CHECKOUT_SUCCESS_URL = os.getenv(
        "PADDLE_CHECKOUT_SUCCESS_URL",
        "https://localhost:3000/admin/subscription/success",
    )
    PADDLE_CHECKOUT_CANCEL_URL = os.getenv(
        "PADDLE_CHECKOUT_CANCEL_URL",
        "https://localhost:3000/admin/subscription/cancel",
    )
    LEMONSQUEEZY_API_KEY = os.getenv("LEMONSQUEEZY_API_KEY", "")
    LEMONSQUEEZY_STORE_ID = os.getenv("LEMONSQUEEZY_STORE_ID", "")
    LEMONSQUEEZY_WEBHOOK_SECRET = os.getenv("LEMONSQUEEZY_WEBHOOK_SECRET", "")
    LEMONSQUEEZY_DEFAULT_VARIANT_ID = os.getenv("LEMONSQUEEZY_DEFAULT_VARIANT_ID", "")
    LEMONSQUEEZY_CHECKOUT_SUCCESS_URL = os.getenv(
        "LEMONSQUEEZY_CHECKOUT_SUCCESS_URL",
        "https://localhost:3000/admin/subscription/success",
    )
    LEMONSQUEEZY_CHECKOUT_CANCEL_URL = os.getenv(
        "LEMONSQUEEZY_CHECKOUT_CANCEL_URL",
        "https://localhost:3000/admin/subscription/cancel",
    )
    LEMONSQUEEZY_API_BASE = os.getenv(
        "LEMONSQUEEZY_API_BASE", "https://api.lemonsqueezy.com/v1"
    )
    DATABASE_URL: str = os.getenv("DATABASE_URL")
    NOTIFICATION_SERVICE_URL: str = os.getenv(
        "NOTIFICATION_SERVICE_URL", "http://notification-service:8006"
    )
    NOTIFICATION_SERVICE_TOKEN: str = os.getenv("NOTIFICATION_SERVICE_TOKEN", "")
    FREE_PLAN_ID: int = int(os.getenv("FREE_PLAN_ID", "0"))
    TAX_DEFAULT_RATE: float = float(os.getenv("TAX_DEFAULT_RATE", "0.02"))
    TAX_DEFAULT_CODE: str = os.getenv("TAX_DEFAULT_CODE", "IT_DIGITAL")
    TAX_DEFAULT_JURISDICTION: str = os.getenv(
        "TAX_DEFAULT_JURISDICTION", "Digital/IT Services"
    )
    _tax_map_raw = os.getenv("TAX_RATE_MAP", "{}")
    try:
        TAX_RATE_MAP = {k: float(v) for k, v in json.loads(_tax_map_raw).items()}
    except json.JSONDecodeError:
        TAX_RATE_MAP = {}

    class Config:
        env_file = ".env"


settings = Settings()
