import logging
import os

LOG_DIR = "/app/logs"
LOG_FILE = os.path.join(LOG_DIR, "vpn_service.log")


os.makedirs(LOG_DIR, exist_ok=True)

logger = logging.getLogger("vpn_service")
logger.setLevel(logging.INFO)

fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
fh.setLevel(logging.INFO)

formatter = logging.Formatter(
    "%(asctime)s - %(levelname)s - %(message)s"
)
fh.setFormatter(formatter)

logger.addHandler(fh)

def get_logger():
    return logger
