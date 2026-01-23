import os
from pydantic import BaseModel
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_PATH = os.path.join(BASE_DIR, ".env")
load_dotenv(ENV_PATH)


class Settings(BaseModel):
    DB_HOST: str = os.getenv("DB_HOST", "localhost")
    DB_PORT: int = int(os.getenv("DB_PORT", "5432"))
    DB_NAME: str = os.getenv("DB_NAME", "postgres")
    DB_USER: str = os.getenv("DB_USER", "postgres")
    DB_PASSWORD: str = os.getenv("DB_PASSWORD", "")

    WG_INTERFACE_NAME: str = os.getenv("WG_INTERFACE_NAME", "wg0")
    WG_SERVER_PUBLIC_KEY: str = os.getenv("WG_SERVER_PUBLIC_KEY", "")
    WG_SERVER_ENDPOINT: str = os.getenv("WG_SERVER_ENDPOINT", "")
    WG_CLIENT_NETWORK_PREFIX: str = os.getenv("WG_CLIENT_NETWORK_PREFIX", "10.8.0.")
    WG_CLIENT_NETWORK_CIDR: int = int(os.getenv("WG_CLIENT_NETWORK_CIDR", "24"))
    WG_CLIENT_IP_START: int = int(os.getenv("WG_CLIENT_IP_START", "10"))

    TRIBUTE_WEBHOOK_SECRET: str = os.getenv("TRIBUTE_WEBHOOK_SECRET", "")
    YOOKASSA_WEBHOOK_SECRET: str = os.getenv("YOOKASSA_WEBHOOK_SECRET", "")

    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    ADMIN_TELEGRAM_ID: int = int(os.getenv("ADMIN_TELEGRAM_ID", "0"))



settings = Settings()
