import logging
import os

LOG_DIR = "/app/logs"
VPN_LOG_FILE = os.path.join(LOG_DIR, "vpn_service.log")
YOOKASSA_LOG_FILE = os.path.join(LOG_DIR, "yookassa.log")
HELEKET_LOG_FILE = os.path.join(LOG_DIR, "heleket.log")

os.makedirs(LOG_DIR, exist_ok=True)

formatter = logging.Formatter(
    "%(asctime)s - %(levelname)s - %(message)s"
)

# ===== основной логгер приложения =====
vpn_logger = logging.getLogger("vpn_service")
vpn_logger.setLevel(logging.INFO)

if not vpn_logger.handlers:
    vpn_fh = logging.FileHandler(VPN_LOG_FILE, encoding="utf-8")
    vpn_fh.setLevel(logging.INFO)
    vpn_fh.setFormatter(formatter)
    vpn_logger.addHandler(vpn_fh)


# ===== логгер ЮKassa =====
yookassa_logger = logging.getLogger("yookassa")
yookassa_logger.setLevel(logging.INFO)

if not yookassa_logger.handlers:
    yk_fh = logging.FileHandler(YOOKASSA_LOG_FILE, encoding="utf-8")
    yk_fh.setLevel(logging.INFO)
    yk_fh.setFormatter(formatter)
    yookassa_logger.addHandler(yk_fh)


# ===== логгер Heleket =====
heleket_logger = logging.getLogger("heleket")
heleket_logger.setLevel(logging.INFO)

if not heleket_logger.handlers:
    hk_fh = logging.FileHandler(HELEKET_LOG_FILE, encoding="utf-8")
    hk_fh.setLevel(logging.INFO)
    hk_fh.setFormatter(formatter)
    heleket_logger.addHandler(hk_fh)


def get_logger():
    return vpn_logger


def get_yookassa_logger():
    return yookassa_logger


def get_heleket_logger():
    return heleket_logger
