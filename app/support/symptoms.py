"""
Классификатор симптомов при жалобе «VPN не работает».
Используется только для handshake_state == "fresh": уточняет ответ по формулировке пользователя.
Детерминированный, без сетевых проверок.
"""
from typing import List

# (keywords, symptom_id) — порядок проверки: более специфичные первыми
SITES_NOT_LOADING_PHRASES: List[str] = [
    "сайты не открываются",
    "сайты не грузятся",
    "браузер не открывает",
    "страницы не грузятся",
    "интернет не открывается",
]
SLOW_SPEED_PHRASES: List[str] = [
    "медленно",
    "скорость",
    "тормозит",
    "долго грузит",
]
MEDIA_PROBLEM_PHRASES: List[str] = [
    "видео не работает",
    "ютуб не грузится",
    "картинки не грузятся",
    "медиа не открывается",
]


def classify_vpn_symptom(text: str) -> str:
    """
    По тексту сообщения определяет симптом для уточнённого ответа при handshake_ok.
    Возвращает: sites_not_loading | slow_speed | media_problem | generic_problem.
    """
    if not text or not text.strip():
        return "generic_problem"
    lower = text.strip().lower()
    for phrase in SITES_NOT_LOADING_PHRASES:
        if phrase in lower:
            return "sites_not_loading"
    for phrase in SLOW_SPEED_PHRASES:
        if phrase in lower:
            return "slow_speed"
    for phrase in MEDIA_PROBLEM_PHRASES:
        if phrase in lower:
            return "media_problem"
    return "generic_problem"
