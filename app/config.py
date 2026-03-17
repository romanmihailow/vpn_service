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
    DB_POOL_MIN: int = int(os.getenv("DB_POOL_MIN", "1"))
    DB_POOL_MAX: int = int(os.getenv("DB_POOL_MAX", "20"))
    DB_IP_ALLOC_LOCK_ID: int = int(os.getenv("DB_IP_ALLOC_LOCK_ID", "4242001"))
    # Advisory lock IDs для фоновых задач (single-instance)
    DB_JOB_LOCK_DEACTIVATE_EXPIRED: int = int(os.getenv("DB_JOB_LOCK_DEACTIVATE_EXPIRED", "2001"))
    DB_JOB_LOCK_NOTIFY_EXPIRING: int = int(os.getenv("DB_JOB_LOCK_NOTIFY_EXPIRING", "2002"))
    DB_JOB_LOCK_REVOKE_UNUSED_PROMO: int = int(os.getenv("DB_JOB_LOCK_REVOKE_UNUSED_PROMO", "2003"))
    DB_JOB_LOCK_NO_HANDSHAKE_REMINDER: int = int(os.getenv("DB_JOB_LOCK_NO_HANDSHAKE_REMINDER", "2004"))
    DB_JOB_LOCK_NEW_HANDSHAKE_ADMIN: int = int(os.getenv("DB_JOB_LOCK_NEW_HANDSHAKE_ADMIN", "2005"))
    DB_JOB_LOCK_HANDSHAKE_FOLLOWUP: int = int(os.getenv("DB_JOB_LOCK_HANDSHAKE_FOLLOWUP", "2006"))
    DB_JOB_LOCK_WELCOME_AFTER_FIRST_PAYMENT: int = int(os.getenv("DB_JOB_LOCK_WELCOME_AFTER_FIRST_PAYMENT", "2007"))
    DB_JOB_LOCK_CONFIG_CHECKPOINT: int = int(os.getenv("DB_JOB_LOCK_CONFIG_CHECKPOINT", "2008"))
    DB_JOB_LOCK_RECENTLY_EXPIRED_TRIAL_FOLLOWUP: int = int(
        os.getenv("DB_JOB_LOCK_RECENTLY_EXPIRED_TRIAL_FOLLOWUP", "2009")
    )
    DB_JOB_LOCK_HANDSHAKE_SHORT_CONFIRMATION: int = int(
        os.getenv("DB_JOB_LOCK_HANDSHAKE_SHORT_CONFIRMATION", "2010")
    )
    ENABLE_HANDSHAKE_SHORT_CONFIRMATION: bool = (
        os.getenv("ENABLE_HANDSHAKE_SHORT_CONFIRMATION", "0") in ("1", "true", "True")
    )

    WG_INTERFACE_NAME: str = os.getenv("WG_INTERFACE_NAME", "wg0")
    WG_SERVER_PUBLIC_KEY: str = os.getenv("WG_SERVER_PUBLIC_KEY", "")
    WG_SERVER_ENDPOINT: str = os.getenv("WG_SERVER_ENDPOINT", "")
    WG_CLIENT_NETWORK_PREFIX: str = os.getenv("WG_CLIENT_NETWORK_PREFIX", "10.8.0.")
    WG_CLIENT_NETWORK_CIDR: int = int(os.getenv("WG_CLIENT_NETWORK_CIDR", "24"))
    WG_CLIENT_IP_START: int = int(os.getenv("WG_CLIENT_IP_START", "10"))
    WG_CONFIG_LOCK_PATH: str = os.getenv("WG_CONFIG_LOCK_PATH", "/tmp/wg0.conf.lock")

    TRIBUTE_WEBHOOK_SECRET: str = os.getenv("TRIBUTE_WEBHOOK_SECRET", "")
    YOOKASSA_WEBHOOK_SECRET: str = os.getenv("YOOKASSA_WEBHOOK_SECRET", "")

    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    ADMIN_TELEGRAM_ID: int = int(os.getenv("ADMIN_TELEGRAM_ID", "0"))



settings = Settings()
