import os
from dotenv import load_dotenv

load_dotenv()


class Settings:
    STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
    STRIPE_PUBLIC_KEY = os.getenv("STRIPE_PUBLIC_KEY")
    STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
    DATABASE_URL: str = os.getenv("DATABASE_URL")
    NOTIFICATION_SERVICE_URL: str = os.getenv(
        "NOTIFICATION_SERVICE_URL", "http://notification-service:8006"
    )
    NOTIFICATION_SERVICE_TOKEN: str = os.getenv("NOTIFICATION_SERVICE_TOKEN", "")

    class Config:
        env_file = ".env"


settings = Settings()
