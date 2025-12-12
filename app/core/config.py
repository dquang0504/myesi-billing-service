import os
from dotenv import load_dotenv

load_dotenv()


class Settings:
    STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
    STRIPE_PUBLIC_KEY = os.getenv("STRIPE_PUBLIC_KEY")
    STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
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

    class Config:
        env_file = ".env"


settings = Settings()
